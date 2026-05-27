#!/bin/bash
# Mycelium Plex Transcoder wrapper.
# Rewrites -i /plex-media/*.mkv to http://127.0.0.1:8088/spore-stream/<token>
# so FFmpeg reads a moov-first (fast-start) MP4 from the Mycelium proxy,
# bypassing the need for LD_PRELOAD interception in musl-based Plex builds.

newargs=()
found_i=0
for a in "$@"; do
    if [ "$found_i" = "1" ]; then
        found_i=0
        if [[ "$a" == *.mkv ]]; then
            minfo="${a%.mkv}.minfo"
            if [ -f "$minfo" ]; then
                tok=$(grep "^token=" "$minfo" | head -1 | cut -d= -f2)
                if [ -n "$tok" ]; then
                    echo "SPORE-WRAP: -i $a -> http://127.0.0.1:8088/spore-stream/$tok" >&2
                    a="http://127.0.0.1:8088/spore-stream/$tok"
                fi
            fi
        fi
    fi
    [ "$a" = "-i" ] && found_i=1
    newargs+=("$a")
done

exec '/usr/lib/plexmediaserver/Plex Transcoder.real' "${newargs[@]}"
