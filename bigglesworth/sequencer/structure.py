import os
from bisect import bisect_left
from xml.etree import ElementTree as ET
import xml.dom.minidom as minidom
import numpy as np

os.environ['QT_PREFERRED_BINDING'] = 'PyQt4'

from Qt import QtCore

from bigglesworth.utils import sanitize
from bigglesworth.midiutils import NOTE, NOTEON, NOTEOFF, CTRL, SYSEX, SYSRT_STOP, MidiEvent
from bigglesworth.parameters import Parameters
from bigglesworth.sequencer.const import (NoteParameter, CtrlParameter, SysExParameter, BlofeldParameter, ParameterTypeMask, 
    Bar, Marker, Tempo, Meter, TimelineEventMask, BLOFELD, 
    BeatHUnit, noteNamesWithPitch, MinimumLength, getCtrlNameFromMapping)


def eventTimeComparison(a, b):
    #ensure that note off events take precedence over same-note on events
    time1, event1 = a
    time2, event2 = b
    if time1 == time2 and event1.eventType & NOTE and event2.eventType & NOTE and \
        event1.note == event2.note:
            return -1 if event1.eventType == NOTEOFF else 1
    diff = time1 - time2
    if not diff:
        return 0
    elif diff < 0:
        return -1
    return 1

def automationTypeComparison(a, b):
    if a.parameterType != b.parameterType:
        return -1 if a.parameterType == BlofeldParameter else 1
    return a.parameterId - b.parameterId


class MetaEvent(QtCore.QObject):
    timeChanged = QtCore.pyqtSignal()

    def __init__(self, eventType, time=0):
        QtCore.QObject.__init__(self)
        self.eventType = eventType
        self._time = time

    @property
    def time(self):
        return self._time

    @time.setter
    def time(self, time):
        if time == self._time:
            return
        self._time = time
        self.timeChanged.emit()

    @property
    def channel(self):
        return self.midiEvent.channel

    @channel.setter
    def channel(self, channel):
        if channel != self.midiEvent.channel:
            self.midiEvent.channel = channel

    def setChannel(self, channel):
        self.midiEvent.channel = channel


class MetaNoteEvent(MetaEvent):
    velocityChanged = QtCore.pyqtSignal(int)

    def __init__(self, eventType, note, time):
        MetaEvent.__init__(self, eventType, time)
        self.noteName = noteNamesWithPitch[note]

    def clone(self, time=None):
        return self.__class__(self.note, self.velocity, self.channel, time if time is not None else self.time)

    @property
    def note(self):
        return self.midiEvent.data1

    @property
    def velocity(self):
        return self.midiEvent.data2

    @velocity.setter
    def velocity(self, velocity):
        velocity = sanitize(0, velocity, 127)
        self.midiEvent.data2 = velocity
        self.velocityChanged.emit(velocity)

    def setNote(self, note):
        note = sanitize(0, int(note), 127)
        self.noteName = noteNamesWithPitch[note]
        self.midiEvent.data1 = note

    def setVelocity(self, velocity):
        if velocity == self.midiEvent.data2:
            return
        self.velocity = velocity


class NoteOnEvent(MetaNoteEvent):
    noteChanged = QtCore.pyqtSignal(int)

    def __init__(self, note=60, velocity=127, channel=0, time=0):
        MetaNoteEvent.__init__(self, NOTEON, note, time)
        self.midiEvent = MidiEvent(NOTEON, 1, channel, data1=note, data2=velocity)

    def setNote(self, note):
        MetaNoteEvent.setNote(self, note)
        self.noteChanged.emit(note)


class NoteOffEvent(MetaNoteEvent):
    def __init__(self, note=60, velocity=0, channel=0, time=0):
        MetaNoteEvent.__init__(self, NOTEOFF, note, time)
        self.midiEvent = MidiEvent(NOTEOFF, 1, channel, note, velocity)


class BlofeldEvent(MetaEvent):
    valueChanged = QtCore.pyqtSignal(int)

    def __init__(self, parameterId, value, part=0, time=0):
        MetaEvent.__init__(self, BLOFELD, time)
        self.parameterId = parameterId
        self.part = part
        self.value = value
        parameter = Parameters.parameterData[parameterId >> 4 & 511]
        if parameter.children:
            self.parameter = parameter.children[parameterId & 7]
        else:
            self.parameter = parameter

    def setValue(self, value):
        value = self.parameter.range.sanitize(value)
        if value != self.value:
            self.value = value
            self.valueChanged.emit(self.value)

    def clone(self):
        return BlofeldEvent(self.parameterId, self.value, self.part, self.time)


class CtrlEvent(MetaEvent):
    valueChanged = QtCore.pyqtSignal(int)

    def __init__(self, ctrl=0, value=0, channel=0, time=0):
        MetaEvent.__init__(self, CTRL, time)
        self.midiEvent = MidiEvent(CTRL, 1, channel, ctrl, value)

    @property
    def ctrl(self):
        return self.midiEvent.data1

    @property
    def value(self):
        return self.midiEvent.data2

    def setValue(self, value):
        value = sanitize(0, value, 127)
        self.midiEvent.data2 = value
        self.valueChanged.emit(value)

    def clone(self):
        return CtrlEvent(self.ctrl, self.value, self.channel, self.time)

    def setCtrl(self, ctrl):
        self.midiEvent.data1 = ctrl


class SysExEvent(MetaEvent):
    def __init__(self, mask, valueIndexes, value, time=0, func=None):
        MetaEvent.__init__(self, SYSEX, time)
        self.midiEvent = MidiEvent(SYSEX, 1, None, None, None, mask)
        self.mask = mask
        if isinstance(valueIndexes, (tuple, list)):
            if len(valueIndexes) == 1:
                valueIndexes = valueIndexes[0]
            else:
                self.valueStart = valueIndexes[0]
                self.valueEnd = valueIndexes[-1]
        if isinstance(valueIndexes, int):
            self.valueStart = valueIndexes
            self.valueEnd = valueIndexes + 1
        self.func = func if func else lambda v: [v] * (self.valueEnd - self.valueStart)
        self.setValue(value)

    def setMask(self, mask):
        self.mask = mask
        self.setValue(self.value)

    def setValueIndexes(self, valueIndexes):
        self.valueStart, self.valueEnd = valueIndexes
        self.setValue(self.value)

    def setFunc(self, func):
        self.func = func
        self.setValue(self.value)

    def setValue(self, value):
        try:
            self.midiEvent.sysex = self.mask[:self.valueStart] + self.func(value) + self.mask[self.valueEnd:]
        except:
            self.midiEvent.sysex = self.mask[:self.valueStart] + [0] * (self.valueEnd - self.valueStart) + self.mask[self.valueEnd:]


class RegionInfo(object):
    def __init__(self, parameterType, parameterId, **kwargs):
        self.parameterType = parameterType
        self.parameterId = parameterId
        self.keys = set()
        for k, v in kwargs.items():
            self.keys.add(k)
            setattr(self, k, v)

    def __iter__(self):
        yield self.parameterType
        yield self.parameterId
        try:
            yield self.mapping
        except:
            pass
        try:
            yield self.extData
        except:
            pass
        for key in sorted(self.keys):
            if key not in ('mapping', 'extData'):
                yield getattr(self, key)

    def __getitem__(self, index):
        for i, item in enumerate(self):
            try:
                if index == i:
                    return item
            except:
                return None

    def __getslice__(self, start, end):
        for index, item in enumerate(self):
            try:
                if start <= index <= end:
                    yield item
            except:
                yield None

    def __str__(self):
        if self.parameterType == BlofeldParameter:
            parameter = Parameters.parameterData[self.parameterId >> 4 & 511]
            if parameter.children:
                parameter = parameter.children[self.parameterId & 7]
            text = parameter.fullName
            if self.parameterId >> 15:
                text += ' (part {})'.format((self.parameterId >> 15) + 1)
            return text
        try:
            return getCtrlNameFromMapping(self.parameterId, self.mapping, True)[0]
        except:
            return getCtrlNameFromMapping(self.parameterId, '', True)[0]

    def __eq__(self, other):
        return self.parameterType == other.parameterType and self.parameterId == other.parameterId

    def __hash__(self):
        return hash((self.parameterType, self.parameterId))


class MetaRegion(QtCore.QObject):
    #time events are based on a "virtual 0" of the parent pattern?
    changed = QtCore.pyqtSignal(object)
    QuantizeNoteStart, QuantizeNoteEnd, QuantizeNoteStartEnd, QuantizeNoteLength = 1, 2, 3, 4
    QuantizeNotes = 7
    QuantizeCtrl = 8
    QuantizeAll = QuantizeNoteStartEnd | QuantizeCtrl
    parameterType = NoteParameter
    parameterId = -1
    extData = mapping = None
    __kwargs__ = {}

    def __init__(self, pattern):
        QtCore.QObject.__init__(self)
        self.pattern = pattern
        self.events = []

    @property
    def regionInfo(self):
        try:
            return self._regionInfo
        except:
            self._regionInfo = RegionInfo(self.parameterType, self.parameterId, **self.kwargs())
            return self._regionInfo

    def kwargs(self):
        return self.__kwargs__

    def patternEvents(self, pattern=None):
        return [event for event in self.events if 0 <= event.time <= (self.pattern.length if pattern is None else pattern.length)]

    def sort(self):
        self.events.sort(key=lambda e: e.time)
        self.changed.emit(self)

    def moveEvent(self, event, time):
        event.time = max(0, time)
        self.sort()

    def moveEvents(self, event, firstTime):
        deltaTime = max(0, firstTime - self.events[0].time)
        for event in self.events:
            event.time += deltaTime
        self.sort()

    def __quantizeEvent(self, event, numerator, denominator):
        #beat is quarter note, so we just multiply the denominator
        denominator *= 4
        temp = round(event.time * numerator * denominator, 0)
        event.time = temp * numerator / float(denominator)

    def quantize(self, time, numerator, denominator):
        #quantize() is based on "integer" beats, not quarters/beats
        #this is to speed up process when quantizing a lot of events.
        #see https://stackoverflow.com/questions/53679806/fraction-based-quantization-of-a-value
        ratio = float(numerator) / denominator
        return (time + ratio / 2) // ratio * ratio

    def quantizeEvents(self, events=None, numerator=1, denominator=4):
        if events is None:
            events = self.events
        numerator *= 4
        for event in events:
            event.time =  self.quantize(event.time, numerator, denominator)
        self.sort()


class NoteRegion(MetaRegion):
    def __init__(self, pattern=None, test=False):
        MetaRegion.__init__(self, pattern)
        self.notePairs = {}
        if test:
            for time, note in enumerate([40, 60, 48, 80]):
                self.addNote(note, time=time)

    def cloneFrom(self, other):
        self.events[:] = []
        self.notePairs.clear()
        for source in other.events:
            if isinstance(source, NoteOnEvent):
                noteOnEvent = source.clone()
                self.events.append(noteOnEvent)
                noteOffEvent = other.notePairs[source].clone()
                self.events.append(noteOffEvent)
                self.notePairs[noteOnEvent] = noteOffEvent
        self.sort()

    def notes(self):
        return sorted(self.notePairs.items(), key=lambda (noteOnEvent, noteOffEvent): noteOnEvent.time)

    def patternEvents(self, pattern=None):
        if pattern is None:
            pattern = self.pattern
        events = []
        for noteOnEvent, noteOffEvent in self.notePairs.items():
            if noteOffEvent.time < 0 or noteOnEvent.time > pattern.length:
                continue
            if noteOnEvent.time < 0:
                noteOnEvent = noteOnEvent.clone(time=0)
            elif noteOffEvent.time > pattern.length:
                noteOffEvent = noteOffEvent.clone(time=pattern.length)
            events.extend((noteOnEvent, noteOffEvent))
        return events

    def noteLength(self, noteEvent):
        if isinstance(noteEvent, NoteOffEvent):
            for noteOnEvent, noteOffEvent in self.notePairs.items():
                if noteOffEvent == noteEvent:
                    break
            else:
                raise 'Note Off not found?!'
        else:
            noteOnEvent = noteEvent
            noteOffEvent = self.notePairs[noteOnEvent]
        return noteOffEvent.time - noteOnEvent.time

    def addNote(self, note, velocity=127, channel=0, length=1, time=0, offVelocity=0):
        noteOnEvent = NoteOnEvent(note, velocity, channel, time)
        noteOffEvent = NoteOffEvent(note, offVelocity, channel, time + length)
        self.notePairs[noteOnEvent] = noteOffEvent
        self.events.extend((noteOnEvent, noteOffEvent))
        self.sort()
        return noteOnEvent, noteOffEvent

    def checkNotesAfterImport(self):
        noteOnEvents = []
        noteOffEvents = {}
        for event in self.events:
            if isinstance(event, NoteOnEvent):
                noteOnEvents.append(event)
            elif isinstance(event, NoteOffEvent):
                try:
                    noteOffEvents[event.note].append(event)
                except:
                    noteOffEvents[event.note] = [event]
            else:
                print('Event discarded! {}'.format(event))
                continue
        noteOnEvents.sort(key=lambda e: e._time)
        [ev.sort(key=lambda e: e._time) for ev in noteOffEvents.values()]
        for noteOnEvent in noteOnEvents:
            try:
                noteOffEvent = noteOffEvents.get(noteOnEvent.note)[0]
                if noteOffEvent._time < noteOnEvent._time:
                    self.events.remove(noteOffEvents[noteOnEvent.note].pop(0))
                    noteOffEvent = NoteOffEvent(noteOnEvent.note, 0, noteOnEvent.channel, noteOnEvent._time + 1)
                else:
                    self.notePairs[noteOnEvent] = noteOffEvents[noteOnEvent.note].pop(0)
            except Exception as e:
                print('Event ignored?! {} {}'.format(noteOnEvent, e))
                self.events.remove(noteOnEvent)
        self.sort()

    def deleteNote(self, noteOnEvent):
        try:
            noteOffEvent = self.notePairs.pop(noteOnEvent)
            self.events.remove(noteOffEvent)
        except:
            pass
        self.events.remove(noteOnEvent)

    def moveNote(self, noteOnEvent, note=None, time=None):
        if note is None and time is None:
            return
        if note is not None:
            noteOnEvent.setNote(note)
            self.notePairs[noteOnEvent].setNote(note)
        if time is not None:
            time = max(0, time)
            diff = time - noteOnEvent.time
            noteOnEvent.time = time
            self.notePairs[noteOnEvent].time += diff
        self.sort()

    def moveNotesBy(self, noteEvents, deltaNote=None, deltaTime=None):
        if deltaNote is None and deltaTime is None:
            return
        for noteOnEvent in noteEvents:
            noteOffEvent = self.notePairs[noteOnEvent]
            if deltaNote is not None:
                noteOnEvent.setNote(noteOnEvent.note + deltaNote)
                noteOffEvent.setNote(noteOnEvent.note)
            if deltaTime is not None:
                noteOnEvent.time = max(0, noteOnEvent.time + deltaTime)
                noteOffEvent.time = noteOffEvent.time + deltaTime
        self.sort()

    def setNoteStart(self, noteOnEvent, time):
        noteOffEvent = self.notePairs[noteOnEvent]
        noteOnEvent.time = sanitize(0, time, noteOffEvent.time - MinimumLength)
        self.sort()

    def setNoteEnd(self, noteEvent, time):
        if isinstance(noteEvent, NoteOffEvent):
            for noteOnEvent, noteOffEvent in self.notePairs.items():
                if noteOffEvent == noteEvent:
                    break
            else:
                raise 'Note Off not found?!'
        else:
            noteOnEvent = noteEvent
            noteOffEvent = self.notePairs[noteOnEvent]
        noteOffEvent.time = max(noteOnEvent.time + MinimumLength, time)
        self.sort()

    def setNoteLength(self, noteOnEvent, length):
        self.notePairs[noteOnEvent].time = max(noteOnEvent.time + length, noteOnEvent.time + MinimumLength)
        self.sort()

    def quantizeEvents(self, events=None, numerator=1, denominator=4):
        if events is None:
            events = self.events
        numerator *= 4
        minimumLength = numerator / float(denominator)
        for event in events:
            if isinstance(event, NoteOnEvent):
                event.time =  self.quantize(event.time, numerator, denominator)
                noteOffEvent = self.notePairs[event]
                noteOffEvent.time = max(event.time + minimumLength, self.quantize(noteOffEvent.time, numerator, denominator))
        self.sort()

    def quantizeNotes(self, events=None, startRatio=(1, 4), endRatio=None, quantizeMode=None):
        if events is None:
            events = self.events
        if startRatio is None and endRatio is None:
            raise 'Specify start or end at least!'
        if startRatio is not None:
            startNumerator, startDenominator = startRatio
            startNumerator *= 4
        if endRatio is None:
            endNumerator, endDenominator = startNumerator, startDenominator
        else:
            endNumerator, endDenominator = endRatio
            endNumerator *= 4
        if quantizeMode is None:
            quantizeMode = self.QuantizeAll
        if quantizeMode & self.QuantizeNoteLength:
            minimumLength = endNumerator / float(endDenominator)
        if quantizeMode & self.QuantizeAll and startRatio is None:
            startNumerator, startDenominator = endNumerator, endDenominator
        for event in events:
            if isinstance(event, NoteOnEvent):
                noteOffEvent = self.notePairs[event]
                if quantizeMode & self.QuantizeNoteStartEnd:
                    event.time =  self.quantize(event.time, endNumerator, endDenominator)
                    noteOffEvent.time = max(event.time + MinimumLength, self.quantize(noteOffEvent.time, endNumerator, endDenominator))
                elif quantizeMode & self.QuantizeNoteStart:
                    event.time =  self.quantize(event.time, endNumerator, endDenominator)
                    noteOffEvent.time = max(event.time + MinimumLength, noteOffEvent.time)
                elif quantizeMode & self.QuantizeNoteEnd:
                    noteOffEvent.time = max(self.quantize(event.time + MinimumLength, endNumerator, endDenominator), 
                        self.quantize(noteOffEvent.time, endNumerator, endDenominator))
                elif quantizeMode & self.QuantizeNoteLength:
                    noteOffEvent.time = max(event.time + minimumLength, 
                        event.time + self.quantize((noteOffEvent.time - event.time), endNumerator, endDenominator))
#            elif quantizeMode & self.QuantizeAll and not isinstance(event, NoteOffEvent):
#                event.time =  self.quantize(event.time, startNumerator, endDenominator)
        self.sort()


class ParameterRegion(MetaRegion):
    continuous = False
    continuousModeChanged = QtCore.pyqtSignal(bool)

    def __init__(self, pattern, parameterType=CtrlParameter, parameterId=0):
        MetaRegion.__init__(self, pattern)
        self.parameterType = parameterType
        self.parameterId = parameterId
        #TODO: needs further developing!

    def removeEvents(self, events):
        for event in events:
            self.events.remove(event)
        self.sort()

    def setContinuous(self, continuous):
        if self.continuous == continuous:
            return
        self.continuous = continuous
        self.continuousModeChanged.emit(continuous)

    def patternEvents(self, pattern=None):
        events = [event for event in self.events if 0 <= event.time <= (self.pattern.length if pattern is None else pattern.length)]
        if self.continuous and len(events) > 1:
            eventIter = iter(events)
            current = eventIter.next()
            next = eventIter.next()
            newEvents = []
            while True:
                extent = abs(current.value - next.value)
                try:
                    if extent > 1:
                        if isinstance(self, CtrlParameterRegion):
                            isCtrl = True
                            step = 1
                        else:
                            isCtrl = False
                            step = self.parameter.range.step
                            assert extent > step
                        if current.value < next.value:
                            iterator = range(current.value + 1, next.value, step)
                        else:
                            iterator = range(current.value - 1, next.value, -1, step)
                        timeRatio = float(next.time - current.time) / extent
                        currentTime = current.time + timeRatio
                        for i, value in enumerate(iterator, 1):
                            if isCtrl:
                                event = CtrlEvent(current.ctrl, value, current.channel, currentTime)
                            else:
                                event = BlofeldEvent(current.parameterId, value, current.part, currentTime)
                            newEvents.append(event)
                            currentTime += timeRatio
                except:
                    pass
                try:
                    current = next
                    next = eventIter.next()
                except:
                    break
            events.extend(newEvents)
            events.sort(key=lambda event: event.time)
        return events

    def moveEventsBy(self, events, deltaValue=None, deltaTime=None):
        for event in events:
            if deltaValue is not None:
                event.setValue(event.value + deltaValue)
            if deltaTime is not None:
                event.time = max(0, event.time + deltaTime)
        self.sort()

#    def clone(self, pattern=None):
#        eventRegion = ParameterRegion(pattern if pattern is not None else self.pattern, self.parameterType, self.parameterId)
#        eventRegion.events[:] = [event.clone() for event in self.events]
#        eventRegion.continuous = self.continuous
#        return eventRegion

    def cloneFrom(self, other):
        self.events[:] = [event.clone() for event in other.events]
        self.continuous = other.continuous
        self.sort()


class BlofeldParameterRegion(ParameterRegion):
    def __init__(self, pattern, parameterId, part=0):
        ParameterRegion.__init__(self, pattern, BlofeldParameter)
        self.parameterId = parameterId
        self.part = parameterId >> 15
        parameter = Parameters.parameterData[parameterId >> 4 & 511]
        if parameter.children:
            self.parameter = parameter.children[parameterId & 7]
        else:
            self.parameter = parameter

    def addEvent(self, value=0, time=0):
        event = BlofeldEvent(self.parameterId, self.parameter.range.sanitize(value), self.part, time)
        self.events.append(event)
        self.sort()
        return event

    def addEvents(self, data):
        for value, time in data:
            self.events.append(BlofeldEvent(self.parameterId, self.parameter.range.sanitize(value), self.part, time))
        self.sort()


class CtrlParameterRegion(ParameterRegion):
    def __init__(self, pattern, parameterId, mapping='Blofeld'):
        ParameterRegion.__init__(self, pattern, CtrlParameter, parameterId)
        self.mapping = mapping
        self.setNameFromMapping(mapping)

    def addEvent(self, value=127, channel=0, time=0):
        event = CtrlEvent(self.parameterId, value, channel, time)
        self.events.append(event)
        self.sort()
        return event

    def addEvents(self, data):
        for value, channel, time in data:
            self.events.append(CtrlEvent(self.parameterId, value, channel, time))
        self.sort()

    def setNameFromMapping(self, mapping):
        self.__kwargs__.update({'mapping': mapping})
        description, valid = getCtrlNameFromMapping(self.parameterId, mapping)
        if valid:
            self.name = description
        else:
            self.name = self.name = 'CC {}'.format(self.parameterId)


class SysExParameterRegion(ParameterRegion):
    def __init__(self, fmt=None):
        pass



AutomationClasses = {
    CtrlParameter: CtrlParameterRegion, 
    SysExParameter: SysExParameterRegion, 
    BlofeldParameter: BlofeldParameterRegion, 
}


class Pattern(QtCore.QObject):
    repetitionsAboutToChange = QtCore.pyqtSignal(int)
    repetitionsChanged = QtCore.pyqtSignal(int)
    regionChanged = QtCore.pyqtSignal(object)
    #time regions should be absolute... then what?
    def __init__(self, track, time=None, length=None, repetitions=1):
        QtCore.QObject.__init__(self)
        self.track = track
        self.structure = track.structure
        self.time = time if time is not None else 0
        self.length = length if length is not None else 4
        self.noteRegion = NoteRegion(self, test=False)
        self.noteRegion.changed.connect(self.regionChanged)
        self.regions = [self.noteRegion]
        self.repetitions = repetitions
        for automationInfo in self.track.automations():
            self.regions.append(AutomationClasses[automationInfo.parameterType](self, *automationInfo[1:]))

    def automations(self):
        return self.track.automations()

    def unloop(self):
        if self.repetitions <= 1:
            return
        self.track.unloopPattern(self)

    def clone(self):
        pattern = Pattern(self.track, self.time, self.length, self.repetitions)
        pattern.noteRegion.cloneFrom(self.noteRegion)
        for eventRegion in self.regions[1:]:
            pattern.getAutomationRegion(eventRegion.regionInfo).cloneFrom(eventRegion)
#            pattern.addRegion(eventRegion.clone(pattern=pattern))
        return pattern

    def copyFrom(self, other):
        self.noteRegion.cloneFrom(other.noteRegion)
        for otherRegion in other.regions[1:]:
            eventRegion = self.getAutomationRegion(otherRegion.regionInfo)
            eventRegion.cloneFrom(otherRegion)

    def notes(self):
        return self.regions[0].notes()

    def setRepetitions(self, repetitions):
        if repetitions != self.repetitions:
            self.repetitions = max(1, repetitions)
            self.repetitionsAboutToChange.emit(self.repetitions)
            self.repetitionsChanged.emit(self.repetitions)

    def moveStartBy(self, delta):
        self.time += delta
        self.length -= delta
        for eventRegion in self.regions:
            for event in eventRegion.events:
                event.time -= delta

    def setChannel(self, channel):
        for eventRegion in self.regions:
            for event in eventRegion.events:
                event.channel = channel

    def getAutomationRegion(self, automationInfo):
        for eventRegion in self.regions[1:]:
            if eventRegion.regionInfo == automationInfo:
                return eventRegion
        else:
            return self.addAutomation(*automationInfo)

    def addAutomation(self, *args, **kwargs):
        if args:
            if isinstance(args[0], RegionInfo):
                newAutomationInfo = args[0]
            else:
                parameterType, parameterId = args[:2]
                try:
                    parameterExtData = args[2]
                    assert isinstance(parameterExtData, dict)
                except:
                    parameterExtData = {}
                newAutomationInfo = RegionInfo(parameterType, parameterId, **parameterExtData)
        else:
            parameterType = kwargs.pop('parameterType')
            parameterId = kwargs.pop('parameterId')
            try:
                parameterExtData = kwargs.pop('extData')
                assert isinstance(parameterExtData, dict)
            except:
                parameterExtData = {}
            newAutomationInfo = RegionInfo(parameterType, parameterId, **parameterExtData)

        regions = self.regions[1:]
        newRegions = []
#        self.regions[1:] = []
        existing = {eventRegion.regionInfo:eventRegion for eventRegion in regions}
        newAutomation = None
        for automationInfo in self.track.automations():
            automation = existing.get(automationInfo)
            if automation:
                existing.pop(automationInfo)
            else:
                if automationInfo == newAutomationInfo:
                    newAutomation = automation = AutomationClasses[newAutomationInfo.parameterType](self, *newAutomationInfo[1:])
                else:
                    automation = AutomationClasses[automationInfo.parameterType](self, *automationInfo[1:])
            newRegions.append(automation)
        for automationInfo in existing.keys():
            eventRegion = existing.pop(automationInfo)
            newRegions.append(eventRegion)
            if automationInfo == newAutomationInfo:
                newAutomation = eventRegion
        if newAutomation is None:
            newAutomation = AutomationClasses[newAutomationInfo.parameterType](
                self, newAutomationInfo.parameterId)
            newRegions.append(newAutomation)
        self.regions[1:] = sorted(newRegions, cmp=automationTypeComparison)
        return newAutomation

    def quantize(self, startRatio, endRatio, otherRatio, quantizeMode):
        if quantizeMode & MetaRegion.QuantizeNotes:
            self.noteRegion.quantizeNotes(startRatio=startRatio, endRatio=endRatio, quantizeMode=quantizeMode)
        if quantizeMode & MetaRegion.QuantizeCtrl:
            numerator, denominator = otherRatio
            for eventRegion in self.regions[1:]:
                self.quantizeEvents(numerator=numerator, denominator=denominator)

    @property
    def events(self):
        print('richiedo eventi')
        events = []
        for eventRegion in self.regions:
            regionEvents = eventRegion.patternEvents(self)
            events.extend(regionEvents)
            deltaTime = 0
            for r in range(self.repetitions - 1):
                deltaTime += self.length
                repEvents = []
                for event in regionEvents:
                    event = event.clone()
                    event.time += deltaTime
                    repEvents.append(event)
                events.extend(repEvents)
#                events.extend(event.time + deltaTime for event in eventRegion.events)
        events.sort(key=lambda e: e.time, cmp=lambda e1, e2: -1 if isinstance(e1, MetaNoteEvent) else 1)
        return events

    def midiEvents(self):
        events = []
        for eventRegion in self.regions:
            events.extend((event.time, event.midiEvent) for event in eventRegion.patternEvents(self))
#        events.sort(key=lambda e: e.time, cmp=lambda e1, e2: -1 if isinstance(e1, MetaNoteEvent) else 1)
        events.sort(cmp=eventTimeComparison)

        tempoIter = iter(self.structure.tempos)
        currentTempo = tempoIter.next()
        try:
            nextTempo = tempoIter.next()
        except:
            nextTempo = None
        currentTime = currentRealTime = 0

        eventDict = {}
        for time, event in events:
            if time != currentTime:
                currentTime = time
                if not nextTempo:
                    currentRealTime = currentTempo.beatLengthMs * time
                else:
                    currentRealTime = currentTempo.beatLengthMs * (nextTempo.time)
                    while nextTempo and nextTempo.time < time:
                        currentTempo = nextTempo
                        try:
                            nextTempo = tempoIter.next()
                            currentRealTime += currentTempo.beatLengthMs * (nextTempo.time - currentTempo.time)
                        except:
                            nextTempo = None
                    currentRealTime += currentTempo.beatLengthMs * (time - currentTempo.time)
            try:
                eventDict[currentRealTime].append(event)
            except:
                eventDict[currentRealTime] = [event]
        return eventDict

    def addRegion(self, region):
        region.changed.connect(self.regionChanged)
        self.regions.append(region)

    def deleteRegion(self, region):
#        region.changed.disconnect(self.regionChanged)
        self.regions.remove(region)


class Track(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    automationAdded = QtCore.pyqtSignal(object)
    automationRemoved = QtCore.pyqtSignal(object)

    def __init__(self, structure, label='', channel=None):
        QtCore.QObject.__init__(self)
        self.structure = structure
        self._channel = min(15, channel if channel is not None else 0)
        self.label = label if label else 'Track {}'.format(self.channel + 1)
        self.patterns = []
        self.knownAutomations = []
#        self.addPattern()

    @property
    def channel(self):
        return self._channel

    @channel.setter
    def channel(self, channel):
        if channel != self._channel:
            self._channel = channel
            for pattern in self.patterns:
                pattern.setChannel(channel)

    def index(self):
        return self.structure.tracks.index(self)

    def addPattern(self, pattern=None, time=None, length=None, repetitions=1):
        if pattern is None:
            pattern = Pattern(self, time, length, repetitions)
        elif pattern.track != self:
            pattern.track = self
        pattern.repetitionsChanged.connect(self.changed)
        pattern.regionChanged.connect(self.changed)
        self.patterns.append(pattern)
        return pattern

    def removePattern(self, pattern):
        self.patterns.remove(pattern)
        return pattern

    def unloopPattern(self, pattern):
        time = pattern.time
        length = pattern.length
        repetitions = pattern.repetitions
        pattern.repetitions = 1
        for loop in range(repetitions - 1):
            time += length
            newPattern = pattern.clone()
            newPattern.time = time
            self.patterns.append(newPattern)
        pattern.repetitionsAboutToChange.emit(repetitions)
        pattern.repetitionsChanged.emit(1)
        self.changed.emit()

    def addAutomation(self, *args, **kwargs):
#        automation = AutomationClasses[kwargs.pop('parameterType')](self, **kwargs)
        if args:
            if isinstance(args[0], RegionInfo):
                automationInfo = args[0]
            else:
                automationInfo = RegionInfo(*args)
        else:
            try:
                parameterExtData = kwargs.pop('extData')
            except:
                parameterExtData = {}
            automationInfo = RegionInfo(kwargs.pop('parameterType'), kwargs.pop('parameterId'), **parameterExtData)
        if automationInfo in self.automations(automationInfo.parameterType):
            return
        self.knownAutomations.append(automationInfo)
        self.knownAutomations.sort(cmp=automationTypeComparison)
        for pattern in self.patterns:
            pattern.addAutomation(automationInfo)
        try:
            return automationInfo
        finally:
            self.automationAdded.emit(automationInfo)

    def automations(self, parameterType=ParameterTypeMask):
        automations = set([a for a in self.knownAutomations if a.parameterType & ParameterTypeMask])
        for pattern in self.patterns:
            for eventRegion in pattern.regions[1:]:
#                if isinstance(eventRegion, NoteRegion):
#                    continue
                if parameterType & ParameterTypeMask:
                    automations.add(eventRegion.regionInfo)
#        #get an automation list without duplicates, while keeping original order
#        existing = set()
#        existingAdd = existing.add
#        return [a for a in automations if not (a in existing or existingAdd(a))]
        return sorted(automations, cmp=automationTypeComparison)


class TimelineEvent(QtCore.QObject):
    eventType = Bar
    timeChanged = QtCore.pyqtSignal(float)
    labelChanged = QtCore.pyqtSignal(str)

    def __init__(self, structure, time, label, first=False):
        QtCore.QObject.__init__(self, structure)
        self.structure = structure
        self._time = time
        self._label = label
        self.first = first

    @property
    def time(self):
        return self._time

    @time.setter
    def time(self, time):
        if not self.first and time != self._time:
            self._time = time
            self.timeChanged.emit(time)

    def setTime(self, time):
        self.time = time

    @property
    def label(self):
        return self._label

    @label.setter
    def label(self, label=''):
        if label != self._label:
            self._label = label
            self.labelChanged.emit(label)

    def setLabel(self, label):
        self.label = label


class TempoEvent(TimelineEvent):
    tempoChanged = QtCore.pyqtSignal(int)
    eventType = Tempo

    def __init__(self, structure, time=0, tempo=120, first=False):
        self._tempo = int(tempo)
        TimelineEvent.__init__(self, structure, time, str(self.tempo), first)
        self.beatLengthMs = 60000. / self.tempo
        self.beatSize = self._tempo / 60000.

    @property
    def tempo(self):
        return self._tempo

    @tempo.setter
    def tempo(self, tempo):
        if tempo != self._tempo:
            self._tempo = int(tempo)
            self.beatLengthMs = 60000. / self._tempo
            self.beatSize = self._tempo / 60000.
            self.tempoChanged.emit(self._tempo)
            self.label = str(self._tempo)

    def setTempo(self, tempo):
        self.tempo = sanitize(30, tempo, 300)


class MeterEvent(TimelineEvent):
    meterChanged = QtCore.pyqtSignal(int, int)
    eventType = Meter

    def __init__(self, structure, time=0, bar=0, numerator=4, denominator=4, first=False):
        self.numerator = int(numerator)
        self.denominator = int(denominator)
        TimelineEvent.__init__(self, structure, time, '{}/{}'.format(self.numerator, self.denominator), first)
        self.beats = float(numerator) / denominator * 4
        self.beatRatio = 1. / numerator
        self._bar = bar

    @property
    def bar(self):
        return self._bar

    @bar.setter
    def bar(self, bar):
        if bar == self._bar:
            return
        for meterEvent in self.structure.meters:
            if meterEvent._bar == bar:
                return
        self._bar = bar
        self.meterChanged.emit(self.numerator, self.denominator)

    @property
    def time(self):
        return self.structure.meterTime(self)

    @time.setter
    def time(self, time):
        self.bar = self.structure.barFromTime(time)
#        if not self.first and time != self._time:
#            for meterEvent in self.structure.meters:
#                if meterEvent.time == time:
#                    return
#            self._time = time
#            self.timeChanged.emit(time)
#            print('haha', self.structure.meterBar(self))

    @property
    def meter(self):
        return self.numerator, self.denominator

    def setMeter(self, numerator, denominator, time=None):
        if time is not None:
            self.time = time
        if numerator != self.numerator or denominator != self.denominator:
            self.numerator = numerator
            self.denominator = denominator
            self.beats = float(numerator) / denominator * 4
            self.beatRatio = 1. / numerator
            self.label = '{}/{}'.format(self.numerator, self.denominator)
            self.meterChanged.emit(self.numerator, self.denominator)

    def __repr__(self):
        return self.label


class MarkerEvent(TimelineEvent):
    eventType = Marker
    def __init__(self, structure, time, label='Marker'):
        TimelineEvent.__init__(self, structure, time, label)


class LoopMarker(TimelineEvent):
    eventType = Marker
    def __init__(self, structure, time):
        TimelineEvent.__init__(self, structure, time, 'Loop')


class LoopStartMarker(LoopMarker):
    pass


class LoopEndMarker(LoopMarker):
    pass


class EndMarker(MarkerEvent):
    def __init__(self, structure, time):
        MarkerEvent.__init__(self, structure, time, 'End')


class Structure(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    automationChanged = QtCore.pyqtSignal(object, object, bool)
    timelineChanged = QtCore.pyqtSignal()
    trackDeleted = QtCore.pyqtSignal(object)
    trackAdded = QtCore.pyqtSignal(object)
    titleChanged = QtCore.pyqtSignal(str)

    def __init__(self):
        QtCore.QObject.__init__(self)
        self.uid = None
        self._title = 'New song'
        self.tracks = []
        tempoEvent = TempoEvent(self, first=True)
        tempoEvent.tempoChanged.connect(self.timelineChanged)
        self.tempos = [tempoEvent]
        meterEvent = MeterEvent(self, first=True)
        meterEvent.meterChanged.connect(self.meterChanged)
        self.meters = [meterEvent]
        self.meterTimes = [0]
        self.meterCoords = [0, 1024], [0, 256]
        self.endMarker = EndMarker(self, 16)
        self.endMarker.timeChanged.connect(self.timelineChanged)
        self.markers = [self.endMarker]
        self.loopStart = self.loopEnd = None
        self.addTrack(Track(self))
        self.timelineChanged.connect(self.checkMeters)
        #the changed signal is delayed for automation as it could interfere with other connections
        self.automationChanged.connect(lambda: QtCore.QTimer.singleShot(0, self.changed.emit))
#        for track in (Track(self), Track(self, channel=3), Track(self, channel=10)):
#            self.addTrack(track)
        self.createdTime = None

    def getSerializedData(self, new=False):
        root = ET.Element('Bigglesworth')

        songElement = ET.SubElement(root, 'SequencerSong')
        songElement.set('title', self.title)

        currentTime = str(QtCore.QDateTime.currentDateTime().toMSecsSinceEpoch())
        songElement.set('edited', currentTime)
        if new or self.createdTime is None:
            songElement.set('created', currentTime)
            self.createdTime = currentTime
        else:
            songElement.set('created', self.createdTime)

        markerListElement = ET.SubElement(songElement, 'Markers')
        for marker in self.markers:
            if isinstance(marker, (LoopMarker, EndMarker)):
                continue
            markerElement = ET.SubElement(markerListElement, 'Marker')
            markerElement.set('time', str(marker.time))
            markerElement.set('label', marker.label)
        endMarkerElement = ET.SubElement(markerListElement, 'End')
        endMarkerElement.set('time', str(self.endMarker.time))

        if self.loopStart and self.loopEnd:
            loopElement = ET.SubElement(songElement, 'Loop')
            loopElement.set('start', str(self.loopStart.time))
            loopElement.set('end', str(self.loopEnd.time))

        tempoListElement = ET.SubElement(songElement, 'Tempos')
        for tempoEvent in self.tempos:
            tempoElement = ET.SubElement(tempoListElement, 'Tempo')
            tempoElement.set('time', str(tempoEvent.time))
            tempoElement.set('tempo', str(tempoEvent.tempo))

        meterListElement = ET.SubElement(songElement, 'Meters')
        for meterEvent in self.meters:
            meterElement = ET.SubElement(meterListElement, 'Meter')
            meterElement.set('time', str(meterEvent.time))
            meterElement.set('bar', str(meterEvent.bar))
            meterElement.set('numerator', str(meterEvent.numerator))
            meterElement.set('denominator', str(meterEvent.denominator))

        for track in self.tracks:
            trackElement = ET.SubElement(songElement, 'Track')
            trackElement.set('label', track.label)
            trackElement.set('channel', str(track.channel))

            autoReference = {}
            automationListElement = ET.SubElement(trackElement, 'Automations')
            for automation in track.automations():
                if automation.parameterType == BlofeldParameter:
                    automationElement = ET.SubElement(automationListElement, 'Blofeld')
                    parameter = Parameters.parameterData[automation.parameterId >> 4 & 511]
                    if parameter.children:
                        parameter = parameter.children[automation.parameterId & 7]
                    automationElement.set('parameter', parameter.attr)
                    automationElement.set('part', str(automation.parameterId >> 15))
                    autoReference[(automation.parameterType, automation.parameterId)] = parameter.attr, str(automation.parameterId >> 15)
                else:
                    automationElement = ET.SubElement(automationListElement, 'CTRL')
                    automationElement.set('parameter', str(automation.parameterId))
                    autoReference[(automation.parameterType, automation.parameterId)] = str(automation.parameterId), None

            patternListElement = ET.SubElement(trackElement, 'Patterns')
            for pattern in track.patterns:
                patternElement = ET.SubElement(patternListElement, 'Pattern')
                patternElement.set('time', str(pattern.time))
                patternElement.set('length', str(pattern.length))
                patternElement.set('repetitions', str(pattern.repetitions))
                regionListElement = ET.SubElement(patternElement, 'Regions')

                noteRegionElement = ET.SubElement(regionListElement, 'Notes')
                for event in pattern.noteRegion.events:
                    if isinstance(event, NoteOnEvent):
                        noteElement = ET.SubElement(noteRegionElement, 'NoteOn')
                    else:
                        noteElement = ET.SubElement(noteRegionElement, 'NoteOff')
                    noteElement.set('note', str(event.note))
                    noteElement.set('velocity', str(event.velocity))
                    noteElement.set('channel', str(event.channel))
                    noteElement.set('time', str(event.time))

                for region in pattern.regions[1:]:
                    if not region.events:
                        continue
                    if region.regionInfo.parameterType == BlofeldParameter:

                        p1, p2 = autoReference[(region.regionInfo.parameterType, region.regionInfo.parameterId)]
                        autoRegionElement = ET.SubElement(regionListElement, 'Blofeld')
                        autoRegionElement.set('parameter', p1)
                        autoRegionElement.set('part', str(p2))
                    else:
                        p1, p2 = autoReference[(region.regionInfo.parameterType, region.regionInfo.parameterId)]
                        autoRegionElement = ET.SubElement(regionListElement, 'CC')
                        autoRegionElement.set('parameter', str(p1))
                        autoRegionElement.set('channel', str(track.channel))
                    autoRegionElement.set('continuous', str(region.continuous))
                    for event in region.events:
                        eventElement = ET.SubElement(autoRegionElement, 'Event')
                        eventElement.set('time', str(event.time))
                        eventElement.set('value', str(event.value))
#        finrire eutto

        data = ET.tostring(root, encoding='utf-8')
#        print(data)
#        print(minidom.parseString(data.encode('utf-8')).toprettyxml())
        return data

    def clearSong(self):
        for track in reversed(self.tracks):
            self.deleteTrack(track)
        if self.loopStart or self.loopEnd:
            self.removeLoop()
        for marker in self.markers + self.tempos[1:] + self.meters[1:]:
            if marker == self.endMarker:
                continue
            self.deleteMarker(marker)

    def newSong(self):
        self.clearSong()
        self.addTrack()
        self.meters[0].setMeter(4, 4)
        self.tempos[0].setTempo(120)
        self.endMarker.time = 16

    def setSerializedData(self, data):
        self.clearSong()
        root = ET.fromstring(data)
        songElement = root.find('SequencerSong')
        assert songElement.find('Track') is not None
        self.title = songElement.get('title')

        markerListElement = songElement.find('Markers')
        endElement = markerListElement.find('End')
        if endElement is not None:
            self.endMarker.time = float(endElement.get('time'))
        for markerElement in markerListElement.findall('Marker'):
            self.addTimelineEvent(Marker, float(markerElement.get('time')), markerElement.get('label'))

        loopElement = songElement.find('Loop')
        if loopElement is not None:
            self.addLoop(float(loopElement.get('start')), float(loopElement.get('end')))


        tempoListElement = songElement.find('Tempos')
        tempoElements = tempoListElement.findall('Tempo')
        tempoElements.sort(key=lambda e: float(e.get('time')))
        self.tempos[0].tempo = float(tempoElements[0].get('tempo', default=120))
        for tempoElement in tempoElements[1:]:
            self.addTimelineEvent(
                Tempo, 
                float(tempoElement.get('time')), 
                float(tempoElement.get('tempo'))
                )

        meterListElement = songElement.find('Meters')
        meterElements = meterListElement.findall('Meter')
        meterElements.sort(key=lambda e: float(e.get('time')))
        self.meters[0].setMeter(int(meterElements[0].get('numerator')), int(meterElements[0].get('denominator')))
        for meterElement in meterElements[1:]:
            self.addTimelineEvent(
                Meter, 
                float(meterElement.get('time')), 
                (int(meterElement.get('numerator', default=4)), int(meterElement.get('denominator', default=4)))
                )

        for trackElement in songElement.findall('Track'):
            channel = int(trackElement.get('channel', default=-1))
            label = trackElement.get('label', default='')
            track = self.addTrack(channel=channel, label=label)

            for automationElement in trackElement.find('Automations').getiterator():
                if automationElement.tag == 'Blofeld':
                    parameter = Parameters.getFromAttribute(automationElement.get('parameter'))
                    if parameter.parent:
                        id = (parameter.parent.id << 4) + parameter.id
                    else:
                        id = parameter.id << 4
                    part = int(automationElement.get('part', default=0))
                    if part:
                        id |= id << 15
                    track.addAutomation(RegionInfo(BlofeldParameter, id))
                elif automationElement.tag == 'CTRL':
                    track.addAutomation(RegionInfo(CtrlParameter, int(automationElement.get('parameter'))))

            patternListElement = trackElement.find('Patterns')
            if patternListElement is None:
                continue
            for patternElement in patternListElement.findall('Pattern'):
                patternTime = float(patternElement.get('time', default=0))
                patternLength = float(patternElement.get('length', default=4))
                patternRepetitions = int(patternElement.get('repetitions', default=1))
                pattern = track.addPattern(time=patternTime, length=patternLength, repetitions=max(1, patternRepetitions))
                regionListElement = patternElement.find('Regions')
                if regionListElement is None:
                    print('vuoto?!')
                    continue

                noteRegionElement = regionListElement.find('Notes')
                if noteRegionElement is not None:
                    noteRegion = pattern.noteRegion
                    for eventElement in noteRegionElement.getiterator():
                        if eventElement == noteRegionElement:
                            continue
                        if eventElement.tag == 'NoteOn':
                            eventClass = NoteOnEvent
                        elif eventElement.tag == 'NoteOff':
                            eventClass = NoteOffEvent
                        else:
                            print('unknown event in NoteRegion?', eventElement, eventElement.tag)
                            continue
                        note = int(eventElement.get('note'))
                        velocity = int(eventElement.get('velocity'))
                        channel = int(eventElement.get('channel'))
                        time = float(eventElement.get('time'))
                        noteRegion.events.append(eventClass(note, velocity, channel, time))
                    noteRegion.checkNotesAfterImport()

                for blofeldElement in regionListElement.findall('Blofeld'):
                    parameterAttr = blofeldElement.get('parameter')
                    parameter = Parameters.getFromAttribute(parameterAttr)
                    parameterId = parameter.id
                    if parameter.parent:
                        parameterId |= parameter.parent.id << 4
                    else:
                        parameterId <<= 4
                    part = int(blofeldElement.get('part', default=0))
                    if part:
                        parameterId |= part << 15

                    eventElements = blofeldElement.findall('Event')
                    eventElements.sort(key=lambda e: float(e.get('time')))
                    eventData = [(int(e.get('value')), float(e.get('time'))) for e in eventElements]
                    for region in pattern.regions[1:]:
                        if region.regionInfo.parameterType == BlofeldParameter and region.regionInfo.parameterId == parameterId:
                            region.addEvents(eventData)
                            break
                    if bool(blofeldElement.get('continuous', default=False)):
                        region.continuous = True

                for ctrlElement in regionListElement.findall('CC'):
                    parameterId = int(ctrlElement.get('parameter'))
                    channel = int(ctrlElement.get('channel'))

                    eventElements = ctrlElement.findall('Event')
                    eventElements.sort(key=lambda e: float(e.get('time')))
                    eventData = [(int(e.get('value')), channel, float(e.get('time'))) for e in eventElements]
                    for region in pattern.regions[1:]:
                        if region.regionInfo.parameterType == CtrlParameter and region.regionInfo.parameterId == parameterId:
                            region.addEvents(eventData)
                            break
                    if bool(ctrlElement.get('continuous', default=False)):
                        region.continuous = True



    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, title):
        if title == self._title:
            return
        self._title = title
        self.titleChanged.emit(self._title)

    @property
    def timelineEvents(self):
        return self.tempos + self.meters + self.markers

    def trackCount(self):
        return len(self.tracks)

    def trackIndex(self, track):
        return self.tracks.index(track)

    def usedChannels(self):
        return sorted(set([track.channel for track in self.tracks]))

    def beatLength(self):
        length = 0
        for track in self.tracks:
            for pattern in track.patterns:
                length = max(length, pattern.time + pattern.length)
        return length

#    def meterBar(self, meterEvent):
#        if meterEvent == self.meters[0]:
#            return 0
#        meterIter = iter(self.meters)
#        currentMeter = meterIter.next()
#        try:
#            nextMeter = meterIter.next()
#            if nextMeter == meterEvent:
#                return int(nextMeter.time / currentMeter.beats)
#        except:
#            raise 'Meter not in structure?!'
#        bar = 0
#        while True:
#            bar += int((nextMeter.time - currentMeter.time) / currentMeter.beats)
#            try:
#                currentMeter = nextMeter
#                nextMeter = meterIter.next()
#                if nextMeter == meterEvent:
#                    return bar + int((nextMeter.time - currentMeter.time) / currentMeter.beats)
#            except:
#                return bar

    def meterTime(self, meterEvent):
        if meterEvent.first:
            return 0
        return self.meterTimes[self.meters.index(meterEvent)]

    def rebuildMeterTimes(self):
        self.meterTimes[:] = [0]
        meterBars = [0]
        self.meters.sort(key=lambda meter: meter._bar)
        meterIter = iter(self.meters)
        currentMeter = meterIter.next()
        currentBar = 0
        currentTime = 0
        while True:
            try:
                nextMeter = meterIter.next()
                nextBar = nextMeter.bar
                currentTime += (nextBar - currentBar) * currentMeter.beats
                self.meterTimes.append(currentTime)
                meterBars.append(nextBar)
                currentMeter = nextMeter
                currentBar = nextBar
            except:
                meterBars.append(currentBar + 1024)
                times = self.meterTimes[:] + [currentTime + currentMeter.beats * 1024]
                self.meterCoords = times, meterBars
#                print(currentBar, currentTime)
                break
        print(self.meterCoords)

    def checkMeters(self):
        meterBars = self.meters[:]
        sortedMeters = sorted(meterBars, key=lambda meter: meter._bar)
        if sortedMeters == self.meters:
            return
        self.meters[:] = sortedMeters
        self.rebuildMeterTimes()
        self.timelineChanged.emit()

    def timeLength(self):
        tempoIter = iter(self.tempos)
        currentTempo = tempoIter.next()
        try:
            nextTempo = tempoIter.next()
        except:
            return currentTempo.beatLengthMs * self.beatLength()
        time = 0
        while True:
            time += (nextTempo.time - currentTempo.time) * currentTempo.beatLengthMs
            currentTempo = nextTempo
            try:
                nextTempo = tempoIter.next()
            except:
                break
        return time + currentTempo.beatLengthMs * (self.beatLength() - currentTempo.beatLengthMs)

    def getTempoLambdas(self, beatSize=BeatHUnit):
        tempoIter = iter(self.tempos)
        currentTempo = tempoIter.next()
        oldCurrentTime = currentTime = 0
        factors = []
        while True:
            try:
                nextTempo = tempoIter.next()
                currentTime += (nextTempo.time - currentTempo.time) * currentTempo.beatLengthMs
                print(currentTime, oldCurrentTime)
                factors.append((currentTime, 
                    lambda t, currentTime=oldCurrentTime, beatSize=currentTempo.beatSize: beatSize * (t - currentTime)))
                oldCurrentTime = currentTime
                currentTempo = nextTempo
            except:
                nextTempo = None
                factors.append((currentTime + 3600000, 
                    lambda t, currentTime=oldCurrentTime, beatSize=currentTempo.beatSize: beatSize * (t - currentTime)))
                break
        return factors

    def sortEvents(self, eventTypeMask=TimelineEventMask):
        if eventTypeMask & Marker:
            self.markers.sort(key=lambda marker: marker.time)
        if eventTypeMask & Tempo:
            self.tempos.sort(key=lambda marker: marker.time)
        if eventTypeMask & Meter:
            self.meters.sort(key=lambda marker: marker._bar)

    def timelineEventTimeChanged(self, eventType=None):
        self.sortEvents(self.sender().eventType)
        self.timelineChanged.emit()

    def meterChanged(self):
        self.rebuildMeterTimes()
        self.timelineChanged.emit()

    def insertTrack(self, index, track):
        track.changed.connect(self.changed)
        self.tracks.insert(index, track)
        return track

    def addTrack(self, track=None, channel=None, label=''):
        if channel < 0:
            channel = min(set(self.usedChannels()) ^ set(range(16)))
        if track is None:
            track = Track(self, channel=channel, label=label)
        track.automationAdded.connect(lambda automation, track=track: self.automationChanged.emit(track, automation, True))
        track.automationRemoved.connect(lambda automation, track=track: self.automationChanged.emit(track, automation, False))
        self.trackAdded.emit(track)
        self.changed.emit()
        return self.insertTrack(len(self.tracks), track)

    def moveTrack(self, track, target):
        index = track.index()
        if index in (target, target - 1):
            return
        self.tracks.remove(track)
        if index > target:
            self.tracks.insert(target, track)
        else:
            self.tracks.insert(target - 1, track)
        self.changed.emit()

    def deleteTrack(self, track):
        self.tracks.remove(track)
        self.trackDeleted.emit(track)
        self.changed.emit()

    def secsFromTime(self, time):
        if not time:
            return time
        tempoIter = iter(self.tempos)
        currentTempo = tempoIter.next()
        currentSecs = 0
        while True:
            try:
                nextTempo = tempoIter.next()
                if time < nextTempo.time:
                    return currentSecs + (time - currentTempo.time) * currentTempo.beatLengthMs
                currentSecs += (nextTempo.time - currentTempo.time) * currentTempo.beatLengthMs
                currentTempo = nextTempo
            except:
                return currentSecs + (time - currentTempo.time) * currentTempo.beatLengthMs

    def barFromTime(self, time):
        if not time:
            return 0
        meterIter = iter(self.meters)
        currentMeter = meterIter.next()
        keepGoing = True
#        currentTime = 0
        bar = 0
        while keepGoing:
            try:
                nextMeter = meterIter.next()
                if time > nextMeter.time:
                    diff = nextMeter.time - currentMeter.time
                    bar += diff / currentMeter.beats
#                    currentTime += diff
                else:
                    break
                currentMeter = nextMeter
            except:
                break
        return int(bar + (time - currentMeter.time) / currentMeter.beats)

    def timeFromBarBeat(self, bar, beat=0):
#        if not bar and not beat:
#            return 0
        barBeats = int(np.interp(bar, self.meterCoords[1], self.meterCoords[0]))
        try:
            meter = self.meters[self.meterTimes.index(barBeats)]
        except:
            meter = self.meters[max(0, bisect_left(self.meterTimes, barBeats) - 1)]
        return barBeats + min(float(beat) * 4 / meter.denominator, meter.beats - meter.beats / meter.numerator)
#        referenceBar = 0
#        for index, meterBar in enumerate(self.meterCoords[1]):
#            if meterBar < bar:
#                referenceBar = meterBar
#            else:
#                break
#        meterIter = iter(self.meters)
#        currentMeter = meterIter.next()
#        currentTime = 0
#        while True:
#            try:
#                nextMeter = meterIter.next()
#                if nextMeter.bar > bar:

    def addLoop(self, start, end):
        if self.loopStart:
            self.setLoop(start, end)
            return
        self.loopStart = LoopStartMarker(self, min(start, end))
        self.loopEnd = LoopEndMarker(self, max(start, end))
        self.markers.extend((self.loopStart, self.loopEnd))
        self.sortEvents(Marker)
        self.loopStart.timeChanged.connect(self.checkLoop)
        self.loopEnd.timeChanged.connect(self.checkLoop)
        self.timelineChanged.emit()

    def removeLoop(self):
        if not self.loopStart:
            return
        self.markers.remove(self.loopStart)
        self.markers.remove(self.loopEnd)
        self.loopStart = self.loopEnd = None
        self.timelineChanged.emit()

    def checkLoop(self):
        if self.loopStart.time >= self.loopEnd.time:
            if self.sender() == self.loopStart:
                self.loopEnd._time = self.loopStart.time + 1
            else:
                self.loopEnd._time = max(1., self.loopEnd._time)
                self.loopStart._time = self.loopEnd.time - 1
        self.timelineEventTimeChanged()

    def setLoop(self, start, end):
        self.loopStart._time = max(0, start)
        self.loopEnd._time = min(self.loopStart._time + 1, end)
        self.timelineChanged.emit()

    def setLoopToFull(self):
        self.loopStart._time = 0
        self.loopEnd._time = self.endMarker.time
        self.timelineChanged.emit()

    def addMarker(self, label, time):
        self.addTimelineEvent(Marker, time, label)

    def addTempo(self, tempo, time):
        self.addTimelineEvent(Tempo, time, tempo)

    def addMeter(self, numerator, denominator, time):
        self.addTimelineEvent(Meter, time, (numerator, denominator))

    def addTimelineEvent(self, eventType, time, data=None):
        if eventType == Marker:
            if data is None:
                label = 'Marker {}'.format(len(self.markers))
            else:
                label = data
            marker = MarkerEvent(self, time, label)
#            marker.timeChanged.connect(self.timelineChanged)
            self.markers.append(marker)
        elif eventType == Tempo:
            if data is None:
                tempo = self.tempos[0].tempo
                for tempoEvent in self.tempos:
                    if tempoEvent.time > time:
                        break
                    tempo = tempoEvent.tempo
            else:
                tempo = data
            marker = TempoEvent(self, time, tempo)
#            marker.timeChanged.connect(self.timelineChanged)
            marker.tempoChanged.connect(self.timelineChanged)
            self.tempos.append(marker)
        else:
            bar = self.barFromTime(time)
            meterBars = [meter.bar for meter in self.meters]
            if bar in meterBars:
                bar += 1
                if bar in meterBars:
                    return
            if data is None:
                numerator, denominator = self.meters[bisect_left(meterBars, bar) - 1].meter
            else:
                numerator, denominator = data
            #bar is the important value here, we just give "time" for 
            #consistency with the base TimelineEvent class
            marker = MeterEvent(self, time, bar, numerator, denominator)
#            marker.timeChanged.connect(self.timelineChanged)
            marker.meterChanged.connect(self.meterChanged)
#            marker.meterChanged.connect(self.rebuildMeterTimes)
            self.meters.append(marker)
            self.rebuildMeterTimes()
        self.sortEvents(eventType)
        marker.timeChanged.connect(self.timelineEventTimeChanged)
        self.timelineChanged.emit()
        return marker

    def deleteMarker(self, marker):
        if isinstance(marker, LoopMarker):
            self.removeLoop()
        elif marker.eventType == Marker:
            self.markers.remove(marker)
        elif marker.eventType == Tempo:
            self.tempos.remove(marker)
        else:
            self.meters.remove(marker)
            self.rebuildMeterTimes()
        self.timelineChanged.emit()

    def deletePattern(self, pattern):
        pattern.track.removePattern(pattern)
        self.changed.emit()

    def deletePatterns(self, patterns):
        for pattern in patterns:
            pattern.track.removePattern(pattern)
        self.changed.emit()

    def midiEvents(self, start=0, endTime=None, pattern=None):
        events = []
        if pattern is not None:
            for event in pattern.events:
                events.append((event.time + pattern.time, event))
        else:
            for track in self.tracks:
                for pattern in track.patterns:
                    for event in pattern.events:
                        events.append((event.time + pattern.time, event))
        events.sort(cmp=eventTimeComparison)

        tempoIter = iter(self.tempos)
        currentTempo = tempoIter.next()
        try:
            nextTempo = tempoIter.next()
        except:
            nextTempo = None
        currentRealTime = 0
        currentTime = 0
        if endTime is None:
            endTime = self.endMarker.time
        if start == endTime:
            return {}

        events.append((endTime, MetaEvent(-1, endTime)))
        eventDict = {}

        startSecs = self.secsFromTime(start)

        for time, event in events:
            if time > endTime:
                break
            if time != currentTime:
                while nextTempo and nextTempo.time < time:
                    currentRealTime += currentTempo.beatLengthMs * (nextTempo.time - currentTime)
                    currentTime = nextTempo.time
                    currentTempo = nextTempo
                    try:
                        nextTempo = tempoIter.next()
                    except:
                        nextTempo = None
            if time < start:
                continue
            currentRealTime += currentTempo.beatLengthMs * (time - currentTime)
            currentTime = time
            try:
                eventDict[currentRealTime - startSecs].append(event)
            except:
                eventDict[currentRealTime - startSecs] = [event]

        #ignore too "short" event regions
        if len(eventDict) == 1 and eventDict.keys()[0] < 10 and all(event.eventType != NOTEON for event in eventDict.values()[0]):
            return {}
        return eventDict

