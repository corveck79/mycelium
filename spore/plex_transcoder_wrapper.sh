#!/bin/bash
# Mycelium Plex Transcoder wrapper.
# Rewrites -i /plex-media/*.mkv to http://127.0.0.1:8088/spore-stream/<token>
# so FFmpeg reads from the CDN directly (MKV) or via moov-first proxy (MP4).

SPORE_LOG=/config/spore-wrap-debug.log
echo "$(date '+%H:%M:%S') WRAP started" >> "$SPORE_LOG"

newargs=()
found_i=0
spore_replaced=0
spore_minfo=""
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
                    spore_replaced=1
                    spore_minfo="$minfo"
                fi
            fi
        fi
    fi
    [ "$a" = "-i" ] && found_i=1
    newargs+=("$a")
done

if [ "$spore_replaced" = "1" ]; then
    # ── Read .minfo options ────────────────────────────────────────────────────
    preferred_audio=""
    if [ -f "$spore_minfo" ]; then
        preferred_audio=$(grep "^preferred_audio=" "$spore_minfo" | head -1 | cut -d= -f2)
    fi
    echo "$(date '+%H:%M:%S') WRAP spore preferred_audio=${preferred_audio:-0}" >> "$SPORE_LOG"

    # ── Remove pre-input EAE TrueHD/DTS-MA decoder hints ──────────────────────
    # The stub declares A_TRUEHD so Plex passes -codec:N truehd_eae before -i,
    # which makes Plex's patched FFmpeg route stream N through the EasyAudio-
    # Encoder TrueHD decoder. But the CDN file often has EAC3 (not TrueHD) at
    # that stream position, causing EAE to fail with "does not start with major
    # sync!". Removing the pre-input EAE hints lets FFmpeg use its own decoder
    # (EAC3 or TrueHD standard), while keeping the post-input -codec:N eac3_eae
    # output encoder (EAE for output is independent of input decode).
    i_pos=-1
    for idx in "${!newargs[@]}"; do
        if [ "${newargs[$idx]}" = "-i" ]; then
            i_pos=$idx
            break
        fi
    done

    if [ "$i_pos" -gt 0 ]; then
        cleaned=()
        skip_next=0
        for idx in "${!newargs[@]}"; do
            if [ "$skip_next" = "1" ]; then
                skip_next=0
                continue
            fi
            arg="${newargs[$idx]}"
            next_idx=$((idx + 1))
            next_arg="${newargs[$next_idx]:-}"

            # Before -i: remove -codec:N truehd_eae / dts_ma_eae pairs
            if [ "$idx" -lt "$i_pos" ] && [[ "$arg" =~ ^-codec:[0-9]+$ ]] && \
               [[ "$next_arg" =~ ^(truehd|dts_ma)_eae$ ]]; then
                skip_next=1
                echo "$(date '+%H:%M:%S') WRAP removed pre-input: $arg $next_arg" >> "$SPORE_LOG"
                echo "SPORE-WRAP: removed pre-input EAE hint: $arg $next_arg" >&2
                continue
            fi
            # Before -i: remove -eae_prefix:N SESSION_ pairs
            if [ "$idx" -lt "$i_pos" ] && [[ "$arg" =~ ^-eae_prefix:[0-9]+$ ]]; then
                skip_next=1
                echo "$(date '+%H:%M:%S') WRAP removed pre-input: $arg $next_arg" >> "$SPORE_LOG"
                echo "SPORE-WRAP: removed pre-input EAE hint: $arg ..." >&2
                continue
            fi
            cleaned+=("$arg")
        done
        newargs=("${cleaned[@]}")
    fi

    # ── Remap audio stream if preferred_audio > 0 ─────────────────────────────
    # Used when CDN has TrueHD at 0:1 AND a decode-safe fallback at 0:(1+N).
    # preferred_audio=N is written to .minfo by Mycelium's probe logic.
    if [ -n "$preferred_audio" ] && [ "$preferred_audio" != "0" ]; then
        stub_audio_idx=1
        cdn_preferred_idx=$((stub_audio_idx + preferred_audio))
        # Add explicit decoder hint for the preferred stream (makes it visible
        # to filter_complex in Plex's patched FFmpeg after EAE hints are gone)
        i_pos2=-1
        for idx in "${!newargs[@]}"; do
            if [ "${newargs[$idx]}" = "-i" ]; then i_pos2=$idx; break; fi
        done
        if [ "$i_pos2" -gt 0 ]; then
            front=("${newargs[@]:0:$i_pos2}")
            back=("${newargs[@]:$i_pos2}")
            newargs=("${front[@]}" "-codec:${cdn_preferred_idx}" "eac3" "${back[@]}")
        fi
        # Replace [0:1] with [0:N] in filter_complex args
        remapped=()
        for arg in "${newargs[@]}"; do
            arg="${arg//\[0:${stub_audio_idx}\]/[0:${cdn_preferred_idx}]}"
            remapped+=("$arg")
        done
        newargs=("${remapped[@]}")
        echo "$(date '+%H:%M:%S') WRAP remapped filter [0:${stub_audio_idx}]->[0:${cdn_preferred_idx}]" >> "$SPORE_LOG"
        echo "SPORE-WRAP: remapped filter_complex [0:${stub_audio_idx}] -> [0:${cdn_preferred_idx}]" >&2
    fi

    # ── Muxer error tolerance ──────────────────────────────────────────────────
    # -max_interleave_delta 0 : video keeps flowing even if audio stalls
    # -max_muxing_queue_size  : bigger buffer for audio seek-sync recovery
    last="${newargs[-1]}"
    unset 'newargs[-1]'
    newargs+=("-max_interleave_delta" "0" "-max_muxing_queue_size" "4096" "$last")
    echo "SPORE-WRAP: injected muxer error-tolerance flags" >&2
    echo "SPORE-WRAP: full command: ${newargs[*]}" >&2
    echo "$(date '+%H:%M:%S') WRAP final cmd: ${newargs[*]}" >> "$SPORE_LOG"
fi

exec '/usr/lib/plexmediaserver/Plex Transcoder.real' "${newargs[@]}"
