#!/bin/bash
# Mycelium Plex Transcoder wrapper.
# Rewrites -i /plex-media/*.mkv to http://127.0.0.1:8088/spore-stream/<token>
# so FFmpeg reads from the CDN directly (MKV) or via moov-first proxy (MP4).

SPORE_LOG=/config/spore-wrap-debug.log
FFMPEG_STDERR_LOG=/config/spore-ffmpeg-stderr.log

# These logs are appended to on every transcode session with no rotation -
# truncate once they cross ~20MB so a long-running Plex container doesn't
# grow them unbounded.
for _log in "$SPORE_LOG" "$FFMPEG_STDERR_LOG"; do
    if [ -f "$_log" ] && [ "$(stat -c%s "$_log" 2>/dev/null || echo 0)" -gt 20971520 ]; then
        : > "$_log"
    fi
done

echo "$(date '+%H:%M:%S') WRAP started" >> "$SPORE_LOG"

# ── EAE_ROOT discovery ─────────────────────────────────────────────────────────
# Plex's patched FFmpeg maps the 'eac3' decoder to eac3_eae, which requires
# EAE_ROOT to point to the EasyAudioEncoder watchfolder. Plex Media Server sets
# this env var when spawning the transcoder, but it is sometimes missing
# (known Plex bug). Discover and export it here as a fallback so EAE can init.
echo "$(date '+%H:%M:%S') WRAP EAE_ROOT=${EAE_ROOT:-(not set)}" >> "$SPORE_LOG"
# EAE_ROOT discovery is deferred to after minfo is read -- only needed for
# EAC3/TrueHD audio (codecs that route through EasyAudioEncoder).
# For AAC/AC3/other codecs we skip the poll entirely to avoid delaying startup.

newargs=()
found_i=0
spore_replaced=0
spore_minfo=""
_strm_tmp_minfo=""
for a in "$@"; do
    if [ "$found_i" = "1" ]; then
        found_i=0
        if [[ "$a" == *.mkv ]]; then
            # Stub MKV path: read .minfo sidecar next to the .mkv file
            minfo="${a%.mkv}.minfo"
            if [ -f "$minfo" ]; then
                tok=$(grep "^token=" "$minfo" | head -1 | cut -d= -f2)
                if [ -n "$tok" ]; then
                    echo "SPORE-WRAP: -i $a -> spore-stream/$tok" >&2
                    a="http://127.0.0.1:8088/spore-stream/$tok"
                    spore_replaced=1
                    spore_minfo="$minfo"
                fi
            fi
        elif [[ "$a" == *.strm ]]; then
            # Plex passes the .strm file PATH to the transcoder.
            # Read the URL from the file, extract token, rewrite to spore-stream.
            strm_url=$(cat "$a" 2>/dev/null | tr -d '[:space:]')
            if [[ "$strm_url" =~ /s(tream|pore-stream)/([a-f0-9]{8,}) ]]; then
                tok="${BASH_REMATCH[2]}"
                echo "SPORE-WRAP: .strm path $a -> token=$tok -> spore-stream" >&2
                a="http://127.0.0.1:8088/spore-stream/$tok"
                spore_replaced=1
                _strm_tmp_minfo=$(mktemp "/tmp/spore-minfo-$tok.XXXXXX")
                curl -sf "http://127.0.0.1:8088/ui/api/spore-minfo/$tok" \
                     -o "$_strm_tmp_minfo" 2>/dev/null \
                     || echo "token=$tok" > "$_strm_tmp_minfo"
                spore_minfo="$_strm_tmp_minfo"
                echo "$(date '+%H:%M:%S') WRAP .strm path: token=$tok minfo fetched" >> "$SPORE_LOG"
            fi
        elif [[ "$a" =~ /s(tream|pore-stream)/([a-f0-9]{8,}) ]]; then
            # Plex passes the stream URL directly as -i (fallback for URL-based inputs).
            tok="${BASH_REMATCH[2]}"
            echo "SPORE-WRAP: -i stream URL tok=$tok -> spore-stream/$tok" >&2
            a="http://127.0.0.1:8088/spore-stream/$tok"
            spore_replaced=1
            _strm_tmp_minfo=$(mktemp "/tmp/spore-minfo-$tok.XXXXXX")
            curl -sf "http://127.0.0.1:8088/ui/api/spore-minfo/$tok" \
                 -o "$_strm_tmp_minfo" 2>/dev/null \
                 || echo "token=$tok" > "$_strm_tmp_minfo"
            spore_minfo="$_strm_tmp_minfo"
            echo "$(date '+%H:%M:%S') WRAP stream URL: token=$tok minfo fetched" >> "$SPORE_LOG"
        fi
    fi
    [ "$a" = "-i" ] && found_i=1
    newargs+=("$a")
done

# Clean up temp minfo file at exit
[ -n "$_strm_tmp_minfo" ] && trap "rm -f '$_strm_tmp_minfo'" EXIT

if [ "$spore_replaced" = "1" ]; then
    # ── Read .minfo options ────────────────────────────────────────────────────
    preferred_audio=""
    if [ -f "$spore_minfo" ]; then
        preferred_audio=$(grep "^preferred_audio=" "$spore_minfo" | head -1 | cut -d= -f2)
    fi
    echo "$(date '+%H:%M:%S') WRAP spore preferred_audio=${preferred_audio:-0}" >> "$SPORE_LOG"

    # ── Detect audio output mode (copy vs transcode) ──────────────────────────
    # Must be done BEFORE modifying args, as it drives the EAE strategy:
    #
    #   copy    (Direct Stream): Shield TV + AV receiver via eARC.
    #           No audio decode needed. Remove eac3_eae hint + -eae_prefix so
    #           EAE is never started. Audio packets copied straight from CDN.
    #
    #   transcode (e.g. ac3): MiTV / Android TV / phone without passthrough.
    #           EAC3 must be decoded and re-encoded. Keep eac3_eae hint AND
    #           -eae_prefix so EAE can find its watchfolder and decode properly.
    #           Never force-copy here: codec mismatch kills the Plex session.
    #
    # _audio_stream_idx: the stream index Plex selected for audio output.
    # With multi-track stubs (real languages) the user may pick track 2 (NLD),
    # 3 (ITA), etc. -- not always index 1. Detect the first post-input -codec:N
    # where N >= 1; that is the selected audio stream index.
    _audio_output_is_copy=0
    _audio_stream_idx=1
    _past_i_aod=0
    for _idx in "${!newargs[@]}"; do
        [ "${newargs[$_idx]}" = "-i" ] && _past_i_aod=1 && continue
        if [ "$_past_i_aod" = "1" ] && [[ "${newargs[$_idx]}" =~ ^-codec:([1-9][0-9]*)$ || "${newargs[$_idx]}" =~ ^-codec:[a-z]:[0-9]+$ ]]; then
            _audio_stream_idx="${BASH_REMATCH[1]}"
            [ "${newargs[$((_idx+1))]:-}" = "copy" ] && _audio_output_is_copy=1
            break
        fi
    done
    echo "$(date '+%H:%M:%S') WRAP audio_output_is_copy=${_audio_output_is_copy} audio_stream_idx=${_audio_stream_idx}" >> "$SPORE_LOG"

    # ── Remove pre-input EAE decoder hints ────────────────────────────────────
    # Plex injects decoder hints before -i based on the stub audio codec:
    #   A_EAC3 stub  -> -codec:1 eac3_eae  (EAE IPC decoder)
    #   A_TRUEHD stub -> -codec:1 truehd_eae
    #   A_PCM stub    -> -codec:1 pcm_s16le
    #
    # When audio output is COPY (Direct Stream): remove eac3_eae + -eae_prefix.
    # No decode needed, so no EAE. We also inject the native decoder (below) as
    # a safety net, though copy mode ignores the decoder.
    #
    # When audio output is TRANSCODE (e.g. EAC3->AC3 for MiTV): KEEP eac3_eae
    # and -eae_prefix. EAE IPC is needed to decode EAC3. Removing -eae_prefix
    # causes eac3_eae to write WAV files to a path EAE cannot find -> timeout.
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

            # Before -i: EAE decoder hints (eac3_eae, truehd_eae) and PCM hints.
            # For COPY (Direct Stream): remove them -- no decode needed, inject
            #   native decoder below as safety net.
            # For TRANSCODE (MiTV AC3 etc.): KEEP eac3_eae + -eae_prefix so EAE
            #   can decode EAC3. Only remove PCM hints (obsolete, never needed).
            if [ "$idx" -lt "$i_pos" ] && \
               { [[ "$arg" =~ ^-codec:[0-9]+$ ]] || [[ "$arg" =~ ^-codec:[a-z]:[0-9]+$ ]]; } && \
               [[ "$next_arg" =~ ^((eac3|truehd|dts_ma)_eae|pcm_s[0-9]+(le|be))$ ]]; then
                # Always remove PCM hints. For EAE hints: only remove on copy mode.
                _is_pcm=0
                [[ "$next_arg" =~ ^pcm_s[0-9]+(le|be)$ ]] && _is_pcm=1
                if [ "$_is_pcm" = "1" ] || [ "$_audio_output_is_copy" = "1" ] || [ -n "$_strm_tmp_minfo" ]; then
                    skip_next=1
                    stream_n="${arg#-codec:}"
                    removed_eae_indices+=("$stream_n")
                    echo "$(date '+%H:%M:%S') WRAP removed pre-input: $arg $next_arg" >> "$SPORE_LOG"
                    echo "SPORE-WRAP: removed pre-input hint: $arg $next_arg" >&2
                    continue
                else
                    echo "$(date '+%H:%M:%S') WRAP kept pre-input: $arg $next_arg (transcode mode, EAE needed)" >> "$SPORE_LOG"
                fi
            fi
            # Before -i: remove -eae_prefix ONLY when audio output is copy.
            # When transcoding (e.g. EAC3->AC3 for MiTV), -eae_prefix tells
            # eac3_eae which watchfolder to use. Removing it causes EAE timeout.
            if [ "$idx" -lt "$i_pos" ] && { [[ "$arg" =~ ^-eae_prefix:[0-9]+$ ]] || [[ "$arg" =~ ^-eae_prefix:[a-z]:[0-9]+$ ]]; }; then
                if [ "$_audio_output_is_copy" = "1" ] || [ -n "$_strm_tmp_minfo" ]; then
                    skip_next=1
                    echo "$(date '+%H:%M:%S') WRAP removed -eae_prefix: $arg $next_arg (copy mode, no EAE)" >> "$SPORE_LOG"
                    echo "SPORE-WRAP: removed EAE prefix hint: $arg" >&2
                    continue
                else
                    echo "$(date '+%H:%M:%S') WRAP kept -eae_prefix: $arg $next_arg (transcode mode, EAE needs it)" >> "$SPORE_LOG"
                fi
            fi
            cleaned+=("$arg")
        done
        newargs=("${cleaned[@]}")
    fi

    # ── Inject native decoder hint (copy mode only) ──────────────────────────
    # Only inject a native decoder hint when audio output is COPY (Direct Stream).
    # In copy mode the eac3_eae hint was removed above, so we add a lightweight
    # native hint as a safety net (FFmpeg ignores decoder hints for copy mode
    # anyway, but it prevents unknown-option errors if something slips through).
    #
    # When audio output is TRANSCODE (e.g. EAC3->AC3 for MiTV):
    #   eac3_eae hint was KEPT above, and -eae_prefix was KEPT, so EAE handles
    #   the decode. Do NOT inject a second native decoder here -- it would
    #   conflict with eac3_eae and break the EAE IPC pipeline.
    cdn_audio_codec=""
    if [ -f "$spore_minfo" ]; then
        cdn_audio_codec=$(grep "^cdn_audio_codec=" "$spore_minfo" | head -1 | cut -d= -f2)
    fi
    # ── EAE_ROOT discovery (only for EAC3/TrueHD) ─────────────────────────────
    # EAE IPC only initialises when Plex Transcoder runs with a local file.
    # With an HTTP URL (-i http://...) EAE never creates its watchfolder, so
    # polling is futile. We still attempt a quick lookup via the PMS process
    # environment in case EAE was already initialised by a prior local session.
    # Only relevant for codecs that route through EAE (eac3, truehd).
    # Determine if EAE is needed: cdn_audio_codec requires EAE input decoding,
    # OR the post-input args contain eac3_eae/truehd_eae as output encoder
    # (e.g. Shield TV requests EAC3 output for AV receiver via eARC).
    _needs_eae=0
    # EAE is needed only for TrueHD/DTS-MA when native decoder is also absent.
    # EAC3 is handled by the native eac3 decoder (injected below), not EAE.
    # _needs_eae is used only to trigger force-audio-copy fallback when EAE_ROOT
    # is absent -- a safety net for edge cases, normally a no-op.
    case "$cdn_audio_codec" in truehd|dts_hd_ma) _needs_eae=1 ;; esac
    # Stale PCM metadata: stub was updated to PCM by probe but then regenerated
    # back to EAC3. Plex still thinks audio is PCM so it sends pcm_s16le decoder
    # hint and does NOT start EAE. CDN has EAC3 -> eac3_eae auto-selected ->
    # fails without watchfolder. Treat as EAE-needed so force-copy fires below.
    if [ "$_is_pcm" = "1" ] && [ "$_audio_output_is_copy" = "0" ]; then
        _needs_eae=1
        echo "$(date '+%H:%M:%S') WRAP stale PCM metadata: force audio copy (cdn_audio_codec=${cdn_audio_codec:-unknown})" >> "$SPORE_LOG"
    fi
    if [ "$_needs_eae" = "0" ]; then
        _after_i=0
        for _a in "${newargs[@]}"; do
            [ "$_a" = "-i" ] && _after_i=1 && continue
            if [ "$_after_i" = "1" ] && [[ "$_a" =~ ^(eac3|truehd)_eae$ ]]; then
                _needs_eae=1
                echo "$(date '+%H:%M:%S') WRAP EAE detected in output encoder: $_a" >> "$SPORE_LOG"
                break
            fi
        done
    fi

    if [ "$_needs_eae" = "1" ]; then
        if [ -z "$EAE_ROOT" ]; then
            # Methode 1: lees EAE_ROOT uit PMS process environment
            for _pid in $(pgrep -f "Plex Media Server" 2>/dev/null | head -5); do
                [ -r "/proc/$_pid/environ" ] || continue
                _val=$(tr '\0' '\n' < "/proc/$_pid/environ" 2>/dev/null \
                       | grep "^EAE_ROOT=" | cut -d= -f2- | head -1)
                if [ -n "$_val" ] && [ -d "$_val" ]; then
                    export EAE_ROOT="$_val"
                    echo "$(date '+%H:%M:%S') WRAP EAE_ROOT from PMS env: $EAE_ROOT" >> "$SPORE_LOG"
                    break
                fi
            done
        fi
        # Methode 2 (find) verwijderd: vond altijd stale watchfolder-dirs van
        # eerdere sessies. Plex herstart creëert dezelfde UUID-map in /run/,
        # maar de EAE daemon draait dan niet meer op dat pad. Een stale
        # EAE_ROOT is erger dan geen: force-audio-copy wordt dan niet
        # getriggerd, eac3_eae schrijft naar de dode watchfolder en hangt
        # tot Plex de sessie na ~5 sec beëindigt.
        if [ -z "$EAE_ROOT" ]; then
            echo "$(date '+%H:%M:%S') WRAP WARNING: EAE_ROOT not set -- EAE will likely fail" >> "$SPORE_LOG"
        fi
    fi

    if [ -n "$cdn_audio_codec" ]; then
        # Helper: returns 0 if stream N has output codec=copy (post-input).
        # When output is copy FFmpeg never decodes the stream, so no decoder hint
        # is needed -- and injecting one can cause EAE to initialise and fail.
        _output_is_copy() {
            local n=$1 after_i=0 idx nidx
            for idx in "${!newargs[@]}"; do
                [ "${newargs[$idx]}" = "-i" ] && after_i=1 && continue
                if [ "$after_i" = "1" ] && [ "${newargs[$idx]}" = "-codec:${n}" ]; then
                    nidx=$((idx + 1))
                    [ "${newargs[$nidx]:-}" = "copy" ] && return 0
                fi
            done
            return 1
        }

        i_pos_n=-1
        for idx in "${!newargs[@]}"; do
            if [ "${newargs[$idx]}" = "-i" ]; then i_pos_n=$idx; break; fi
        done
        # Inject native decoder only when audio output is copy -- EAE not needed,
        # native decoder prevents eac3_eae from being auto-selected on HTTP input.
        # Stale PCM + EAC3 CDN case is handled above via force-copy (_needs_eae=1).
        if [ "$i_pos_n" -gt 0 ] && { [ "$_audio_output_is_copy" = "1" ] || [ -n "$_strm_tmp_minfo" ]; }; then
            front=("${newargs[@]:0:$i_pos_n}")
            back=("${newargs[@]:$i_pos_n}")
            # Use removed EAE stream indices if available; otherwise default to 1
            inject_indices=("${removed_eae_indices[@]}")
            if [ ${#inject_indices[@]} -eq 0 ]; then
                inject_indices=(1)
            fi
            native_hints=()
            for ei in "${inject_indices[@]}"; do
                if _output_is_copy "$ei"; then
                    # Output is copy: no decode happens, no decoder hint needed.
                    # Injecting one would trigger EAE init which fails on HTTP input.
                    echo "$(date '+%H:%M:%S') WRAP skip decoder hint :${ei} (output=copy)" >> "$SPORE_LOG"
                    echo "SPORE-WRAP: skip decoder hint -codec:${ei} (output=copy, EAE not needed)" >&2
                    continue
                fi
                native_hints+=("-codec:${ei}" "$cdn_audio_codec")
                echo "$(date '+%H:%M:%S') WRAP inject native decoder: -codec:${ei} ${cdn_audio_codec}" >> "$SPORE_LOG"
                echo "SPORE-WRAP: injected native decoder: -codec:${ei} ${cdn_audio_codec}" >&2
            done
            newargs=("${front[@]}" "${native_hints[@]}" "${back[@]}")
        fi
    fi

    # ── Force video copy when Plex chose full transcode ───────────────────────
    # VP8 stub forces Plex to transcode video (no client Direct Plays VP8).
    # The actual CDN video (HEVC, H264) is copied as-is by FFmpeg.
    # Detect when post-input -codec:0 is not "copy" and restructure:
    #   - Remove video filter_complex ([0:0]scale...hwupload/yuv420p)
    #   - Remove -init_hw_device / -filter_hw_device
    #   - Replace -map [video_hw_label] with -map 0:0
    #   - Replace -codec:0 <encoder> with -codec:0 copy
    #   - Remove video encoding params (bitrate, preset, keyframe, etc.)
    _vcodec_post=""
    _ai=0
    for idx in "${!newargs[@]}"; do
        [ "${newargs[$idx]}" = "-i" ] && _ai=1 && continue
        if [ "$_ai" = "1" ] && [ "${newargs[$idx]}" = "-codec:0" ]; then
            _vcodec_post="${newargs[$((idx+1))]:-}"
            break
        fi
    done

    if [ -n "$_vcodec_post" ] && [ "$_vcodec_post" != "copy" ]; then
        echo "$(date '+%H:%M:%S') WRAP force video copy (was: $_vcodec_post)" >> "$SPORE_LOG"
        echo "SPORE-WRAP: forcing video copy (was: $_vcodec_post)" >&2
        _vhl=""
        _fc=()
        _sk=0
        _past_i=0
        for idx in "${!newargs[@]}"; do
            [ "$_sk" -gt 0 ] && { _sk=$((_sk-1)); continue; }
            _a="${newargs[$idx]}"
            _n="${newargs[$((idx+1))]:-}"
            [ "$_a" = "-i" ] && _past_i=1
            case "$_a" in
                -fps_mode|-init_hw_device|-filter_hw_device)
                    _sk=1; continue ;;
                -filter_complex)
                    if [[ "$_n" == \[0:0\]* ]] || [[ "$_n" == \[0:V:0\]* ]]; then
                        _vhl=$(echo "$_n" | grep -oE '\[[0-9]+\]' | tail -1)
                        _sk=1
                        echo "$(date '+%H:%M:%S') WRAP removed video filter_complex (label=${_vhl})" >> "$SPORE_LOG"
                        continue
                    fi ;;
                -map)
                    if [ -n "$_vhl" ] && [ "$_n" = "$_vhl" ]; then
                        _fc+=("-map" "0:0"); _sk=1
                        echo "$(date '+%H:%M:%S') WRAP replaced -map ${_vhl} -> 0:0" >> "$SPORE_LOG"
                        continue
                    fi ;;
                -codec:0)
                    # Only replace post-input: pre-input is a decoder hint (keep as-is)
                    if [ "$_past_i" = "1" ] && [ "$_n" != "copy" ]; then
                        _fc+=("-codec:0" "copy"); _sk=1; continue
                    fi ;;
                -b:0|-maxrate:0|-bufsize:0|-force_key_frames:0|-crf:0|-preset:0|-level:0|-x264opts:0|-x265opts:0)
                    _sk=1; continue ;;
                -sei:0|-a53_cc)
                    continue ;;
            esac
            _fc+=("$_a")
        done
        newargs=("${_fc[@]}")
        echo "$(date '+%H:%M:%S') WRAP video copy forced OK" >> "$SPORE_LOG"
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

    # ── Force audio copy: EAE-unavailable fallback only ──────────────────────
    # For EAC3 CDN with Direct Stream (copy) output: _acodec_post=copy ->
    #   inner condition fails -> no-op.
    # For EAC3 CDN with transcode output (MiTV): eac3_eae + -eae_prefix KEPT
    #   above -> EAE decodes EAC3 -> AC3 output -> correct, no force-copy needed.
    # Force-copy only fires as a last-resort when EAE is truly absent (e.g.
    #   TrueHD/DTS-MA on a system without EAE_ROOT). In that case a copy is
    #   better than a hang, even if it causes a codec mismatch.
    _force_audio_copy=0
    if [ "$_needs_eae" = "1" ] && [ -z "$EAE_ROOT" ]; then
        _force_audio_copy=1
        echo "$(date '+%H:%M:%S') WRAP force audio copy: EAE unavailable (fallback)" >> "$SPORE_LOG"
    fi
    if [ "$_force_audio_copy" = "1" ]; then
        _acodec_post=""
        _ai3=0
        for idx in "${!newargs[@]}"; do
            [ "${newargs[$idx]}" = "-i" ] && _ai3=1 && continue
            if [ "$_ai3" = "1" ] && [ "${newargs[$idx]}" = "-codec:${_audio_stream_idx}" ]; then
                _acodec_post="${newargs[$((idx+1))]:-}"
                break
            fi
        done

        if [ -n "$_acodec_post" ] && [ "$_acodec_post" != "copy" ]; then
            echo "$(date '+%H:%M:%S') WRAP force audio copy (was: $_acodec_post, stream:${_audio_stream_idx}, EAE unavailable)" >> "$SPORE_LOG"
            _ahl2=""
            _fa2=()
            _sk3=0
            _past_i3=0
            for idx in "${!newargs[@]}"; do
                [ "$_sk3" -gt 0 ] && { _sk3=$((_sk3-1)); continue; }
                _a="${newargs[$idx]}"
                _n="${newargs[$((idx+1))]:-}"
                [ "$_a" = "-i" ] && _past_i3=1
                case "$_a" in
                    -filter_complex)
                        if [[ "$_n" == \[0:${_audio_stream_idx}\]* ]] || [[ "$_n" == \[0:a:0\]* ]]; then
                            _ahl2=$(echo "$_n" | grep -oE '\[[0-9]+\]' | tail -1)
                            _sk3=1
                            echo "$(date '+%H:%M:%S') WRAP removed audio filter_complex (label=${_ahl2})" >> "$SPORE_LOG"
                            continue
                        fi ;;
                    -map)
                        if [ -n "$_ahl2" ] && [ "$_n" = "$_ahl2" ]; then
                            _fa2+=("-map" "0:${_audio_stream_idx}"); _sk3=1
                            echo "$(date '+%H:%M:%S') WRAP replaced audio -map ${_ahl2} -> 0:${_audio_stream_idx}" >> "$SPORE_LOG"
                            continue
                        fi ;;
                    *)
                        if [ "$_past_i3" = "1" ]; then
                            if [ "$_a" = "-codec:${_audio_stream_idx}" ] && [ "$_n" != "copy" ]; then
                                _fa2+=("-codec:${_audio_stream_idx}" "copy"); _sk3=1; continue
                            fi
                            if [ "$_a" = "-b:${_audio_stream_idx}" ] || \
                               [ "$_a" = "-maxrate:${_audio_stream_idx}" ] || \
                               [ "$_a" = "-bufsize:${_audio_stream_idx}" ]; then
                                _sk3=1; continue
                            fi
                        fi ;;
                esac
                _fa2+=("$_a")
            done
            newargs=("${_fa2[@]}")

            # Also remove pre-input audio decoder hints injected earlier.
            # With -codec:1 copy output, no decoder is needed. But the
            # injected -codec:1 eac3 hint causes Plex's FFmpeg to open
            # eac3_eae (its aliased decoder) even for copy mode, which
            # fails with "No EAE watchfolder set!" on HTTP input.
            _fa3=()
            _past_i_ah=0
            _sk_ah=0
            for idx in "${!newargs[@]}"; do
                [ "$_sk_ah" -gt 0 ] && { _sk_ah=$((_sk_ah-1)); continue; }
                _a="${newargs[$idx]}"
                _n="${newargs[$((idx+1))]:-}"
                [ "$_a" = "-i" ] && _past_i_ah=1
                if [ "$_past_i_ah" = "0" ] && [[ "$_a" =~ ^-codec:[1-9] ]]; then
                    _sk_ah=1
                    echo "$(date '+%H:%M:%S') WRAP removed pre-input audio hint: $_a $_n (audio copy)" >> "$SPORE_LOG"
                    continue
                fi
                _fa3+=("$_a")
            done
            newargs=("${_fa3[@]}")
            echo "$(date '+%H:%M:%S') WRAP audio copy forced OK" >> "$SPORE_LOG"
        fi
    fi

    # ── Make subtitle stream mappings optional ─────────────────────────────────
    # CDN MKV stream layout may differ from stub metadata. If Plex maps 0:2 for
    # subtitles but the CDN file has no stream at index 2, FFmpeg exits with error.
    # Append '?' to -map 0:N specifiers in the subtitle output section (after
    # media-%05d.ts) so FFmpeg silently skips missing streams instead of crashing.
    _past_first_out=0
    _sub_optional_count=0
    for idx in "${!newargs[@]}"; do
        _a="${newargs[$idx]}"
        [[ "$_a" == *"media-%05d"* ]] && _past_first_out=1
        if [ "$_past_first_out" = "1" ] && [ "$_a" = "-map" ]; then
            _nxt="${newargs[$((idx+1))]:-}"
            if [[ "$_nxt" =~ ^0:[0-9]+$ ]]; then
                newargs[$((idx+1))]="${_nxt}?"
                _sub_optional_count=$((_sub_optional_count+1))
                echo "$(date '+%H:%M:%S') WRAP sub-map optional: ${_nxt} -> ${_nxt}?" >> "$SPORE_LOG"
            fi
        fi
    done
    [ "$_sub_optional_count" -gt 0 ] && \
        echo "$(date '+%H:%M:%S') WRAP made $_sub_optional_count sub-map(s) optional" >> "$SPORE_LOG"

    # ── Muxer error tolerance ──────────────────────────────────────────────────
    # -max_interleave_delta 0 : video keeps flowing even if audio stalls
    # -max_muxing_queue_size  : bigger buffer for audio seek-sync recovery
    last="${newargs[-1]}"
    unset 'newargs[-1]'
    newargs+=("-max_interleave_delta" "0" "-max_muxing_queue_size" "4096" "$last")
    # ── Override loglevel for stderr capture ──────────────────────────────────
    # Plex passes -loglevel quiet which suppresses all FFmpeg output including
    # errors. Temporarily override to 'error' so failures are visible in the
    # spore-ffmpeg-stderr.log. Remove once root cause is found.
    for idx in "${!newargs[@]}"; do
        if [ "${newargs[$idx]}" = "-loglevel" ] && [ "${newargs[$((idx+1))]:-}" = "quiet" ]; then
            newargs[$((idx+1))]="error"
            echo "$(date '+%H:%M:%S') WRAP override -loglevel quiet->error (debug)" >> "$SPORE_LOG"
        fi
    done

    echo "SPORE-WRAP: injected muxer error-tolerance flags" >&2
    echo "SPORE-WRAP: full command: ${newargs[*]}" >&2
    echo "$(date '+%H:%M:%S') WRAP final cmd: ${newargs[*]}" >> "$SPORE_LOG"
fi

if [ "$spore_replaced" = "1" ]; then
    echo "=== $(date '+%H:%M:%S') SPORE session ===" >> "$FFMPEG_STDERR_LOG"
    # The EXIT trap above never fires here: exec replaces this process image
    # instead of letting the shell exit normally, so clean up explicitly first.
    [ -n "$_strm_tmp_minfo" ] && rm -f "$_strm_tmp_minfo"
    exec '/usr/lib/plexmediaserver/Plex Transcoder.real' "${newargs[@]}" \
        2>>"$FFMPEG_STDERR_LOG"
fi
[ -n "$_strm_tmp_minfo" ] && rm -f "$_strm_tmp_minfo"
exec '/usr/lib/plexmediaserver/Plex Transcoder.real' "${newargs[@]}"
