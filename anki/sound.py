# -*- coding: utf-8 -*-
# Copyright: Damien Elmes <anki@ichi2.net>
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import re, sys, threading, time, subprocess, os, atexit
import  random
from anki.hooks import addHook
from anki.utils import  tmpdir, isWin, isMac

# Shared utils
##########################################################################

_soundReg = "\[sound:(.*?)\]"

def playFromText(text):
    for match in re.findall(_soundReg, text):
        play(match)

def stripSounds(text):
    return re.sub(_soundReg, "", text)

def hasSound(text):
    return re.search(_soundReg, text) is not None

##########################################################################

processingSrc = "rec.wav"
processingDst = "rec.mp3"
processingChain = []
recFiles = []

processingChain = [
    ["lame", "rec.wav", processingDst, "--noreplaygain", "--quiet"],
    ]

# don't show box on windows
if isWin:
    si = subprocess.STARTUPINFO()
    try:
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    except:
        # python2.7+
        si.dwFlags |= subprocess._subprocess.STARTF_USESHOWWINDOW
else:
    si = None

if isMac:
    # make sure lame, which is installed in /usr/local/bin, is in the path
    os.environ['PATH'] += ":" + "/usr/local/bin"
    dir = os.path.dirname(os.path.abspath(__file__))
    dir = os.path.abspath(dir + "/../../../..")
    os.environ['PATH'] += ":" + dir + "/Resources/audio"

def retryWait(proc):
    # osx throws interrupted system call errors frequently
    while 1:
        try:
            return proc.wait()
        except OSError:
            continue

# Mplayer settings
##########################################################################

exeDir = os.path.dirname(os.path.abspath(sys.argv[0]))
if isWin:
    os.environ['PATH'] += ";" + exeDir
    mplayerCmd = ["mplayer.exe", "-ao", "win32"]
else:
    os.environ['PATH'] += ":" + exeDir
    mplayerCmd = ["mplayer"]

mplayerCmd += ["-really-quiet", "-noautosub"]

# Mplayer in slave mode
##########################################################################

mplayerQueue = []
mplayerManager = None
mplayerReader = None
mplayerEvt = threading.Event()
mplayerClear = False

class MplayerMonitor(threading.Thread):

    def run(self):
        global mplayerClear
        self.mplayer = None
        self.deadPlayers = []
        while 1:
            mplayerEvt.wait()
            mplayerEvt.clear()
            # clearing queue?
            if mplayerClear and self.mplayer:
                try:
                    self.mplayer.stdin.write("stop\n")
                except:
                    # mplayer quit by user (likely video)
                    self.deadPlayers.append(self.mplayer)
                    self.mplayer = None
            # loop through files to play
            while mplayerQueue:
                # ensure started
                if not self.mplayer:
                    self.startProcess()
                # pop a file
                try:
                    item = mplayerQueue.pop(0)
                except IndexError:
                    # queue was cleared by main thread
                    continue
                if mplayerClear:
                    mplayerClear = False
                    extra = ""
                else:
                    extra = " 1"
                cmd = 'loadfile "%s"%s\n' % (item, extra)
                try:
                    self.mplayer.stdin.write(cmd)
                except:
                    # mplayer has quit and needs restarting
                    self.deadPlayers.append(self.mplayer)
                    self.mplayer = None
                    self.startProcess()
                    self.mplayer.stdin.write(cmd)
                # if we feed mplayer too fast it loses files
                time.sleep(1)
            # wait() on finished processes. we don't want to block on the
            # wait, so we keep trying each time we're reactivated
            def clean(pl):
                if pl.poll() is not None:
                    pl.wait()
                    return False
                else:
                    return True
            self.deadPlayers = [pl for pl in self.deadPlayers if clean(pl)]

    def kill(self):
        if not self.mplayer:
            return
        try:
            self.mplayer.stdin.write("quit\n")
            self.deadPlayers.append(self.mplayer)
        except:
            pass
        self.mplayer = None

    def startProcess(self):
        try:
            cmd = mplayerCmd + ["-slave", "-idle"]
            env = os.environ.copy()
            if not isWin and not isMac:
                env["LD_LIBRARY_PATH"]=exeDir
            self.mplayer = subprocess.Popen(
                cmd, startupinfo=si, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                universal_newlines=True, bufsize=1, env=env)
        except OSError:
            mplayerEvt.clear()
            raise Exception("Did you install mplayer?")

def queueMplayer(path):
    ensureMplayerThreads()
    if isWin and os.path.exists(path):
        # mplayer on windows doesn't like the encoding, so we create a
        # temporary file instead. oddly, foreign characters in the dirname
        # don't seem to matter.
        dir = tmpdir()
        name = os.path.join(dir, "audio%s%s" % (
            random.randrange(0, 1000000), os.path.splitext(path)[1]))
        f = open(name, "wb")
        f.write(open(path, "rb").read())
        f.close()
        # it wants unix paths, too!
        path = name.replace("\\", "/")
    mplayerQueue.append(path)
    mplayerEvt.set()

def clearMplayerQueue():
    global mplayerClear, mplayerQueue
    mplayerQueue = []
    mplayerClear = True
    mplayerEvt.set()

def ensureMplayerThreads():
    global mplayerManager
    if not mplayerManager:
        mplayerManager = MplayerMonitor()
        mplayerManager.daemon = True
        mplayerManager.start()
        # ensure the tmpdir() exit handler is registered first so it runs
        # after the mplayer exit
        tmpdir()
        # clean up mplayer on exit
        atexit.register(stopMplayer)

def stopMplayer(*args):
    if not mplayerManager:
        return
    mplayerManager.kill()

addHook("unloadProfile", stopMplayer)

# PyAudio recording
##########################################################################

import pyaudio
import wave

PYAU_FORMAT = pyaudio.paInt16
PYAU_CHANNELS = 1
PYAU_INPUT_INDEX = None

class _Recorder(object):

    def postprocess(self, encode=True):
        self.encode = encode
        for c in processingChain:
            #print c
            if not self.encode and c[0] == 'lame':
                continue
            try:
                ret = retryWait(subprocess.Popen(c, startupinfo=si))
            except:
                ret = True
            if ret:
                raise Exception(_(
                    "Error running %s") %
                                " ".join(c))

class PyAudioThreadedRecorder(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.finish = False

    def run(self):
        chunk = 1024
        p = pyaudio.PyAudio()

        rate = int(p.get_default_input_device_info()['defaultSampleRate'])

        stream = p.open(format=PYAU_FORMAT,
                        channels=PYAU_CHANNELS,
                        rate=rate,
                        input=True,
                        input_device_index=PYAU_INPUT_INDEX,
                        frames_per_buffer=chunk)

        data = b""
        while not self.finish:
            try:
                data += stream.read(chunk)
            except IOError as e:
                if e[1] == pyaudio.paInputOverflowed:
                    pass
                else:
                    raise
        stream.close()
        p.terminate()
        wf = wave.open(processingSrc, 'wb')
        wf.setnchannels(PYAU_CHANNELS)
        wf.setsampwidth(p.get_sample_size(PYAU_FORMAT))
        wf.setframerate(rate)
        wf.writeframes(data)
        wf.close()

class PyAudioRecorder(_Recorder):

    def __init__(self):
        for t in recFiles + [processingSrc, processingDst]:
            try:
                os.unlink(t)
            except OSError:
                pass
        self.encode = False

    def start(self):
        self.thread = PyAudioThreadedRecorder()
        self.thread.start()

    def stop(self):
        self.thread.finish = True
        self.thread.join()

    def file(self):
        if self.encode:
            tgt = "rec%d.mp3" % time.time()
            os.rename(processingDst, tgt)
            return tgt
        else:
            return processingSrc

# Audio interface
##########################################################################

_player = queueMplayer
_queueEraser = clearMplayerQueue

def play(path):
    _player(path)

def clearAudioQueue():
    _queueEraser()

Recorder = PyAudioRecorder
