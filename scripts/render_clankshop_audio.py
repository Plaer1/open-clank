"""Render Clankshop's original music and foley to PCM assets before deployment."""

import argparse
from array import array
import math
from pathlib import Path
from random import Random
import sys
import wave


RATE = 24_000
TAU = math.tau
SCORES = {
    "workshop": (84, (36, 36, 41, 43), (60, 0, 64, 67, 0, 69, 67, 0, 64, 0, 62, 64, 67, 0, 62, 0)),
    "drift": (116, (45, 43, 41, 43), (69, 0, 72, 74, 76, 0, 74, 72, 67, 69, 0, 72, 0, 67, 64, 0)),
    "drift-pop": (124, (43, 45, 48, 38), (67, 0, 71, 0, 74, 76, 0, 74, 0, 72, 69, 0, 67, 0, 69, 0)),
    "drift-arcade": (120, (40, 45, 49, 47), (76, 0, 0, 80, 0, 83, 0, 0, 85, 0, 0, 83, 0, 80, 0, 0)),
    "idle": (66, (48, 45, 41, 43), (72, 0, 0, 0, 76, 0, 0, 0, 79, 0, 76, 0, 74, 0, 0, 0)),
}


def frequency(note):
    return 440 * 2 ** ((note - 69) / 12)


class Clip:
    def __init__(self, frames, *, loop=False):
        self.samples = [0.0] * frames
        self.loop = loop

    def mix(self, at, seconds, level, attack, shape, *, echo=0):
        start = round(at * RATE)
        frames = round(seconds * RATE)
        length = len(self.samples)
        for index in range(frames):
            position = start + index
            if not self.loop and position >= length:
                break
            time = index / RATE
            phase = min(1, time / attack)
            release = min(1, (frames - 1 - index) / (RATE * min(.07, seconds * .36)))
            envelope = phase * phase * release * release * math.exp(-2.8 * time / seconds)
            value = shape(time) * level * envelope
            self.samples[position % length] += value
            if echo:
                delay = position + round(echo * RATE)
                if delay < length or self.loop:
                    self.samples[delay % length] += value * .20

    def tone(self, at, note, seconds, level, *, end=None, attack=.008, color=0, echo=0):
        first, last = frequency(note), frequency(end if end is not None else note)

        def shape(time):
            angle = TAU * (first * time + (last - first) * time * time / (2 * seconds))
            return math.sin(angle) + color * (.27 * math.sin(2 * angle) + .11 * math.sin(3 * angle))

        self.mix(at, seconds, level, attack, shape, echo=echo)

    def air(self, at, seconds, level, *, cutoff=1700, attack=.004, seed=1):
        random = Random(seed)
        low = narrow = 0
        speed = 1 - math.exp(-TAU * cutoff / RATE)
        damping = 1 - math.exp(-TAU * cutoff * .24 / RATE)

        def shape(_):
            nonlocal low, narrow
            low += speed * (random.uniform(-1, 1) - low)
            narrow += damping * (low - narrow)
            return (low - narrow) * 1.7

        self.mix(at, seconds, level, attack, shape)

    def write(self, directory, name):
        pcm = array("h", (round(32767 * max(-1, min(1, v))) for v in self.samples))
        if sys.byteorder != "little":
            pcm.byteswap()
        with wave.open(str(directory / (name + ".wav")), "wb") as output:
            output.setparams((1, 2, RATE, len(pcm), "NONE", "not compressed"))
            output.writeframes(pcm.tobytes())


def render_score(directory, scene, bpm, bass, motif, *, meter=4, asset=None):
    step = 60 / (4 * bpm)
    bar_steps = 4 * meter
    bed = Clip(round(len(bass) * bar_steps * step * RATE), loop=True)
    activity = Clip(len(bed.samples), loop=True)
    for bar, root in enumerate(bass):
        for index in range(bar_steps):
            note = motif[index] if index < len(motif) else 0
            moment = (bar_steps * bar + index) * step
            seed = 100 + 16 * bar + index
            if scene != "drift-arcade" and bar == 1 and index == 12:
                note = 79 if scene == "drift" else 67
            if scene != "drift-arcade" and bar == 3 and index == 14:
                note = 69 if scene == "drift" else 72

            if scene == "workshop":
                moment += .024 * (index % 2)
                if index in (0, 8):
                    bed.tone(moment, root, .44, .28, color=.56)
                if index in (4, 12):
                    bed.air(moment, .08, .33, cutoff=1780, seed=seed)
                if note:
                    bed.tone(moment, note, .19, .19, color=.70, echo=step * 2)
                if index % 4 == 2:
                    activity.air(moment, .047, .18, cutoff=3300, seed=seed)
                if index in (7, 15):
                    activity.tone(moment, root + 36, .22, .115, color=.7, echo=step * 2)
            elif scene in ("drift-pop", "drift-arcade"):
                arcade = scene == "drift-arcade"
                minor = bar == 2 if arcade else bar in (1, 3)
                if arcade:
                    chord = (0, 3 if minor else 4, 7)
                    if note and bar:
                        note = root + 24 + {0:12, 3:chord[1], 5:7, 8:19, 11:12, 13:chord[1]}[index]
                    if index in (1, 5, 9, 13):
                        activity.tone(moment, root + 36 + chord[(index//4+bar)%3],
                                      .095, .10, color=.91, echo=step*2)
                    if index in (6, 14, 15):
                        activity.air(moment, .055, .085, cutoff=5200, seed=seed+11)
                if arcade and meter != 4:
                    # 3/4 accents three quarter notes. 5/4 emphasizes 3+2.
                    groups = (0, 4, 8) if meter == 3 else (0, 12)
                    if index in groups and index != 0:
                        activity.tone(moment, root + 24, .14, .17, color=.74)
                    if meter == 5 and index in (16, 18):
                        bed.tone(moment, root + (12 if index == 18 else 0), .16, .20, color=.72)
                    if index == bar_steps - 1:
                        activity.air(moment, .042, .14, cutoff=4900, seed=seed+20)
                if index in (0, 3, 6, 8, 11, 14):
                    bed.tone(moment, root + (12 if index in (6, 14) else 0), .16, .22, color=.72)
                if index % 4 == 0:
                    emphasis = 1 if not arcade or meter == 4 or index == 0 or (meter == 5 and index == 12) else .56
                    bed.tone(moment, 41, .15, .30*emphasis, end=28, attack=.004)
                if index in ((4, 8) if arcade and meter == 3 else (4, 12, 16) if arcade and meter == 5 else (4, 12)):
                    bed.air(moment, .12, .31, cutoff=2250, seed=seed)
                if index % 2 == 0:
                    bed.air(moment, .05, .15, cutoff=4100, seed=seed+3)
                if note:
                    bed.tone(moment, note, .23, .15, color=.84, echo=step * 3)
                if index in (2, 7, 10, 15):
                    for semitones in (0, 3 if minor else 4, 7):
                        activity.tone(moment, root + 24 + semitones, .15, .09, color=.76, echo=step * 4)
                if index % 2:
                    activity.air(moment, .042, .12, cutoff=4800, seed=seed)
            elif scene == "drift":
                if index % 2 == 0:
                    bed.tone(moment, root + (12 if index % 8 == 6 else 0), .17, .24, color=.63)
                if index % 4 == 0:
                    bed.tone(moment, 42, .16, .36, end=28, attack=.003)
                if index % 4 == 2:
                    bed.air(moment, .066, .24, cutoff=3500, seed=seed)
                if note:
                    bed.tone(moment, note, .19, .17, color=.38, echo=step * 3)
                if index % 4 == 3:
                    activity.air(moment, .055, .19, cutoff=4100, seed=seed)
                if index in (1, 9, 13):
                    activity.tone(moment, root + 24, .165, .16, color=.86)
            else:
                if index in (0, 10):
                    bed.tone(moment, root, 1.13, .22, attack=.16, echo=step * 2)
                if index in (0, 8):
                    bed.air(moment, .67, .30, cutoff=640, attack=.17, seed=seed)
                if note:
                    bed.tone(moment, note, .73, .16, attack=.034, color=.24, echo=step * 3)
                if index in (7, 11, 15):
                    activity.tone(moment, root + 31, .46, .11, attack=.06, echo=step * 2)
    output = asset or scene
    bed.write(directory, output + "-bed")
    activity.write(directory, output + "-activity")


def render_effects(directory):
    light = Clip(round(.13 * RATE))
    light.tone(0, 77, .086, .25, end=62)
    light.air(.012, .095, .36, cutoff=3600, seed=26)
    light.write(directory, "workshop-light")

    heavy = Clip(round(.21 * RATE))
    heavy.tone(0, 53, .17, .37, end=32, color=.85)
    heavy.air(.025, .115, .32, cutoff=1870, seed=54)
    heavy.write(directory, "workshop-heavy")

    shift = Clip(round(.23 * RATE))
    shift.tone(.013, 53, .19, .25, end=67, color=.8)
    shift.air(0, .185, .33, cutoff=1450, seed=86)
    shift.write(directory, "drift-action")

    idle = Clip(round(.34 * RATE))
    idle.tone(.04, 79, .25, .22, end=74, attack=.025)
    idle.air(0, .30, .39, cutoff=730, attack=.065, seed=14)
    idle.write(directory, "idle-action")

    dodge = Clip(round(.27 * RATE))
    dodge.air(0, .24, .53, cutoff=1280, attack=.019, seed=91)
    dodge.tone(.012, 55, .21, .11, end=49, color=.25)
    dodge.write(directory, "dodge")

    fail = Clip(round(.40 * RATE))
    fail.tone(0, 55, .37, .30, end=43, color=.49)
    fail.air(.013, .18, .28, cutoff=665, seed=4)
    fail.write(directory, "failure")

    chimes = {
        "pickup": ((76, 79, 84), .045, .19, .55),
        "completion": ((67, 72), .080, .35, .58),
        "question": ((74, 69), .095, .22, .27),
        "permission": ((67, 76), .120, .25, .18),
    }
    for name, (notes, spacing, decay, color) in chimes.items():
        sound = Clip(round((decay + spacing * len(notes) + .02) * RATE))
        for index, note in enumerate(notes):
            sound.tone(index * spacing, note, decay, .27 - .035 * index, color=color)
        sound.write(directory, name)


DRIFT_CHANTS = {
    "chip": {"vowel":"Dooo","note":62,"glide":3,"duration":.35,"opening":470,"formant":340,"brightness":850,"echo":.10},
    "coil": {"vowel":"Dooo","note":67,"glide":2,"duration":.34,"opening":510,"formant":365,"brightness":960,"echo":.12},
    "cell": {"vowel":"Dahh","note":65,"glide":4,"duration":.32,"opening":410,"formant":710,"brightness":1430,"echo":.095},
    "star": {"vowel":"Dahh","note":72,"glide":3,"duration":.39,"opening":445,"formant":760,"brightness":1540,"echo":.15},
}


def render_drift_voices(directory, *, chant=False):
    # Original sung vowel effects. A low formant moves into the final vowel
    # over each pitched syllable. Harmonics are baked during authoring.
    voices = {
        "chip": (72, 76, 290, 850, 1860, .23),
        "coil": (74, 79, 380, 490, 2140, .27),
        "cell": (76, 81, 670, 520, 1100, .31),
        "star": (79, 86, 360, 780, 1300, .38),
    }
    if chant:
        voices = {
            name:(p["note"], p["note"] + p["glide"], p["opening"], p["formant"], p["brightness"], p["duration"])
            for name,p in DRIFT_CHANTS.items()
        }
    for name, (first, last, opening, vowel, second, duration) in voices.items():
        echo = DRIFT_CHANTS[name]["echo"] if chant else .07
        clip = Clip(round((duration + max(.12,echo)) * RATE))
        start_hz, end_hz = frequency(first), frequency(last)

        def sung(time):
            glide = min(1, time / .075)
            fundamental = start_hz + (end_hz - start_hz) * glide
            if time < .075:
                cycles = start_hz * time + (end_hz - start_hz) * time * time / .15
            else:
                cycles = (start_hz + end_hz) * .0375 + end_hz * (time - .075)
            cycles += .018 * math.sin(TAU * 6 * time)
            formant = opening + (vowel - opening) * glide
            sample = .22 * math.sin(TAU * cycles)
            for harmonic in range(2, 13):
                partial = fundamental * harmonic
                weight = math.exp(-((partial - formant) / 185) ** 2)
                weight += .53 * math.exp(-((partial - second) / 410) ** 2)
                sample += (weight / math.sqrt(harmonic)) * math.sin(TAU * harmonic * cycles)
            return sample

        clip.mix(0, duration, .43, .015, sung, echo=echo)
        clip.write(directory, ("drift-chant-" if chant else "drift-vocal-") + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Directory for newly rendered WAV files")
    parser.add_argument("--drift-pop-only", action="store_true", help="Render the latest Drift score and pickup voices")
    parser.add_argument("--drift-chant-only", action="store_true", help="Render the four Dooo and Dahh pickup cues")
    parser.add_argument("--drift-arcade-only", action="store_true", help="Render the Drift Arcade bed and activity layer")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.drift_arcade_only:
        for meter in (3, 4, 5):
            asset = "drift-arcade" if meter == 4 else f"drift-arcade-{meter}"
            render_score(args.output, "drift-arcade", *SCORES["drift-arcade"], meter=meter, asset=asset)
        return
    if args.drift_chant_only:
        render_drift_voices(args.output, chant=True)
        return
    for scene, (bpm, bass, motif) in SCORES.items():
        if not args.drift_pop_only or scene == "drift-pop":
            render_score(args.output, scene, bpm, bass, motif)
    if not args.drift_pop_only:
        render_effects(args.output)
    render_drift_voices(args.output)
    render_drift_voices(args.output, chant=True)


if __name__ == "__main__":
    main()
