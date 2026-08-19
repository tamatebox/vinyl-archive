#!/bin/sh
# Generate a test waveform that exercises session detection:
#   30 s silence -> 90 s tone -> 5 s gap (track gap, must NOT split)
#   -> 60 s tone -> 40 s silence (must end the session)
# Output: raw S16_LE stereo PCM on stdout at 48 kHz.
#
# Usage as a capture command override in config.toml:
#   command = ["/bin/sh", "/path/to/tools/make_test_audio.sh"]
# Add "-re" to REALTIME below for real-time pacing; without it the whole
# stream plays as fast as the pipe drains (accelerated integration test).

REALTIME=${REALTIME:-}

exec ffmpeg -hide_banner -loglevel error $REALTIME -f lavfi -i "
aevalsrc=0:d=30 [s1];
sine=frequency=440:duration=90,volume=0.3 [m1];
aevalsrc=0:d=5 [gap];
sine=frequency=550:duration=60,volume=0.3 [m2];
aevalsrc=0:d=40 [s2];
[s1][m1][gap][m2][s2] concat=n=5:v=0:a=1
" -ac 2 -ar 48000 -f s16le -
