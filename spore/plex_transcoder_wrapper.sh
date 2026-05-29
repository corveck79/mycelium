#!/bin/bash
# Mycelium Plex Transcoder wrapper.
# Rewrites -i /plex-media/*.mkv to http://127.0.0.1:8088/spore-stream/<token>
# so FFmpeg reads from the CDN directly (MKV) or via moov-first proxy (MP4).

SPORE_LOG=/config/spore-wrap-debug.log
echo "$(date '+%H:%M:%S') WRAP started" >> "$SPORE_LOG"

# ── EAE_ROOT discovery ─────────────────────────────────────────────────────────
# Plex's patched FFmpeg maps the 'eac3' decoder to eac3_eae, which requires
# EAE_ROOT to point to the EasyAudioEncoder watchfolder. Plex Media Server sets
# this env var when spawning the transcoder, but it is sometimes missing
# (known Plex bug). Discover and export it here as a fallback so EAE can init.
echo "$(date '+%H:%M:%S') WRAP EAE_ROOT=${EAE_ROOT:-(not set)}" >> "$SPORE_LOG"
if [ -z "$EAE_ROOT" ]; then
    _eae_dir=$(find /tmp /var/tmp -maxdepth 5 -type d \
        \( -name "EasyAudioEncoder" -o -name "*EAE*" \) 2>/dev/null | head -1)
    if [ -n "$_eae_dir" ]; then
        export EAE_ROOT="$_eae_dir"
        echo "$(date '+%H:%M:%S') WRAP discovered EAE_ROOT=$EAE_ROOT" >> "$SPORE_LOG"
    else
        echo "$(date '+%H:%M:%S') WRAP WARNING: EAE watchfolder not found in /tmp" >> "$SPORE_LOG"
    fi
fi

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
        removed_eae_indices=()
        for idx in "${!newargs[@]}"; do
            if [ "$skip_next" = "1" ]; then
                skip_next=0
                continue
            fi
            arg="${newargs[$idx]}"
            next_idx=$((idx + 1))
            next_arg="${newargs[$next_idx]:-}"

            # Before -i: remove -codec:N truehd_eae / dts_ma_eae / pcm_s* pairs.
            # Plex injects pcm_s16le (or pcm_s24le etc) when the stub declares PCM
            # audio. The CDN file has EAC3, so this hint is wrong and conflicts with
            # the -codec:N eac3 hint we inject below, causing FFmpeg to crash.
            if [ "$idx" -lt "$i_pos" ] && [[ "$arg" =~ ^-codec:[0-9]+$ ]] && \
               [[ "$next_arg" =~ ^((truehd|dts_ma)_eae|pcm_s[0-9]+(le|be))$ ]]; then
                skip_next=1
                stream_n="${arg#-codec:}"
                removed_eae_indices+=("$stream_n")
                echo "$(date '+%H:%M:%S') WRAP removed pre-input: $arg $next_arg" >> "$SPORE_LOG"
                echo "SPORE-WRAP: removed pre-input hint: $arg $next_arg" >&2
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

    # ── Inject native decoder hint to bypass EAE for input decoding ───────────
    # Plex's patched FFmpeg auto-routes EAC3 input through eac3_eae even without
    # an explicit pre-input hint. Under load (e.g. VAAPI video transcode on
    # Shield TV) this causes EAE timeouts. cdn_audio_codec in .minfo tells us
    # the actual codec so we can inject a native (non-EAE) decoder hint.
    # Injected unconditionally (not just when EAE hints were removed) so it also
    # works for stubs with non-TrueHD audio codecs that don't generate EAE hints.
    cdn_audio_codec=""
    if [ -f "$spore_minfo" ]; then
        cdn_audio_codec=$(grep "^cdn_audio_codec=" "$spore_minfo" | head -1 | cut -d= -f2)
    fi
    if [ -n "$cdn_audio_codec" ]; then
        i_pos_n=-1
        for idx in "${!newargs[@]}"; do
            if [ "${newargs[$idx]}" = "-i" ]; then i_pos_n=$idx; break; fi
        done
        if [ "$i_pos_n" -gt 0 ]; then
            front=("${newargs[@]:0:$i_pos_n}")
            back=("${newargs[@]:$i_pos_n}")
            # Use removed EAE stream indices if available; otherwise default to 1
            inject_indices=("${removed_eae_indices[@]}")
            if [ ${#inject_indices[@]} -eq 0 ]; then
                inject_indices=(1)
            fi
            native_hints=()
            for ei in "${inject_indices[@]}"; do
                native_hints+=("-codec:${ei}" "$cdn_audio_codec")
                echo "$(date '+%H:%M:%S') WRAP inject native decoder: -codec:${ei} ${cdn_audio_codec}" >> "$SPORE_LOG"
                echo "SPORE-WRAP: injected native decoder: -codec:${ei} ${cdn_audio_codec}" >&2
            done
            newargs=("${front[@]}" "${native_hints[@]}" "${back[@]}")
        fi
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
    # Replace -loglevel quiet / -loglevel_plex error with verbose so we can see
    # FFmpeg errors in the Plex log. TEMPORARY DEBUG -- remove after fix confirmed.
    replaced_loglevel=()
    skip_loglevel=0
    for arg in "${newargs[@]}"; do
        if [ "$skip_loglevel" = "1" ]; then
            skip_loglevel=0
            replaced_loglevel+=("verbose")
            continue
        fi
        if [[ "$arg" == "-loglevel" || "$arg" == "-loglevel_plex" ]]; then
            skip_loglevel=1
            replaced_loglevel+=("$arg")
            continue
        fi
        replaced_loglevel+=("$arg")
    done
    newargs=("${replaced_loglevel[@]}")
    newargs+=("-max_interleave_delta" "0" "-max_muxing_queue_size" "4096" "$last")
    echo "SPORE-WRAP: injected muxer error-tolerance flags" >&2
    echo "SPORE-WRAP: full command: ${newargs[*]}" >&2
    echo "$(date '+%H:%M:%S') WRAP final cmd: ${newargs[*]}" >> "$SPORE_LOG"
fi

exec '/usr/lib/plexmediaserver/Plex Transcoder.real' "${newargs[@]}"
