"""Event sounds — synthesized in-process (numpy -> WAV bytes), no asset files.

Each event type gets an unmistakable earcon:
  signal_buy    rising two-tone blip          (dit-DIT ↑)
  signal_sell   falling two-tone blip         (dit-dit ↓)
  fill          quick major arpeggio          (order filled — cash register vibe)
  profit        triumphant major chord swell  (target hit)
  loss          low descending groan          (stopped out)
  council       soft gong                     (new AI Council decision)
"""
from __future__ import annotations

import io
import wave
from functools import lru_cache

import numpy as np

RATE = 22050


def _env(n: int, attack=0.01, release=0.25) -> np.ndarray:
    e = np.ones(n)
    a, r = max(1, int(n * attack)), max(1, int(n * release))
    e[:a] = np.linspace(0, 1, a)
    e[-r:] = np.linspace(1, 0, r)
    return e


def _tone(freq: float, dur: float, vol=0.5, harmonics=(1.0,)) -> np.ndarray:
    t = np.linspace(0, dur, int(RATE * dur), endpoint=False)
    sig = sum((1 / (i + 1)) * np.sin(2 * np.pi * freq * h * t) for i, h in enumerate(harmonics))
    return vol * _env(len(t)) * sig / max(1, len(harmonics) * 0.7)


def _seq(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts)


def _mix(*parts: np.ndarray) -> np.ndarray:
    n = max(len(p) for p in parts)
    out = np.zeros(n)
    for p in parts:
        out[:len(p)] += p
    return out / len(parts) * 1.6


def _square(freq: float, dur: float, vol=0.35) -> np.ndarray:
    """8-bit video-game square wave (odd harmonics) — instant retro-meme feel."""
    t = np.linspace(0, dur, int(RATE * dur), endpoint=False)
    sig = sum(np.sin(2 * np.pi * freq * h * t) / h for h in (1, 3, 5, 7))
    return vol * _env(len(t), release=0.35) * sig


def _womp(f_start: float, f_end: float, dur: float, vol=0.5) -> np.ndarray:
    """Sad-trombone note: downward pitch glide with vibrato, brassy harmonics."""
    n = int(RATE * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    freq = np.linspace(f_start, f_end, n) * (1 + 0.02 * np.sin(2 * np.pi * 5.5 * t))
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    sig = np.sin(phase) + 0.5 * np.sin(2 * phase) + 0.3 * np.sin(3 * phase)
    return vol * _env(n, attack=0.05, release=0.3) * sig / 1.8


def _wav(sig: np.ndarray) -> bytes:
    sig = np.clip(sig, -1, 1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


@lru_cache(maxsize=1)
def sound_bank() -> dict[str, bytes]:
    return {
        # BUY: 8-bit coin collect (B5 -> E6 square wave) — "money get!" 🪙📈
        "signal_buy": _wav(_seq(_square(988, 0.085), _square(1319, 0.33))),
        # SELL: sad trombone "womp womp" 📉
        "signal_sell": _wav(_seq(_womp(247, 220, 0.28), _womp(220, 175, 0.45))),
        "fill": _wav(_seq(_tone(523, 0.08), _tone(659, 0.08), _tone(784, 0.16, harmonics=(1, 2)))),
        "profit": _wav(_mix(_tone(523, 0.55, harmonics=(1, 2)), _tone(659, 0.55, harmonics=(1, 2)),
                            _tone(784, 0.55, harmonics=(1, 2)), _tone(1046, 0.4))),
        "loss": _wav(_seq(_tone(330, 0.2, harmonics=(1, 1.5)), _tone(262, 0.2, harmonics=(1, 1.5)),
                          _tone(220, 0.34, harmonics=(1, 1.5)))),
        "council": _wav(_tone(440, 0.7, vol=0.45, harmonics=(1, 2.76, 5.4))),
        # NEWS ALERT: urgent triple beep — the sentinel pulled the fire alarm
        "alert": _wav(_seq(_tone(880, 0.1, vol=0.6), _tone(880, 0.1, vol=0.6),
                           _tone(1175, 0.22, vol=0.65, harmonics=(1, 2)))),
        # Council verdict sounds: gong + direction melody
        "council_long": _wav(_seq(_tone(440, 0.4, vol=0.4, harmonics=(1, 2.76)),
                                  _tone(523, 0.12), _tone(659, 0.22))),   # gong then rising = LONG
        "council_short": _wav(_seq(_tone(440, 0.4, vol=0.4, harmonics=(1, 2.76)),
                                   _tone(392, 0.12), _tone(311, 0.24))),  # gong then falling = SHORT

    }


if __name__ == "__main__":
    bank = sound_bank()
    for name, data in bank.items():
        assert data[:4] == b"RIFF" and len(data) > 2000, name
        print(f"{name:12s} {len(data):6d} bytes")
    print("sound bank OK —", len(bank), "earcons")
