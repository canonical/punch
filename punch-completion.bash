# Bash completion for punch commands, options, categories, and tasks.

_punch_complete()
{
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prev="${COMP_WORDS[COMP_CWORD-1]}"
    local commands="start add report export login submit config help weeklyeditor whats-new tutorial"
    local config_commands="show edit path set get wizard"
    local dates="today yesterday tomorrow monday tuesday wednesday thursday friday saturday sunday"
    local options=""
    local command="${COMP_WORDS[1]:-}"
    COMPREPLY=()

    if (( COMP_CWORD == 1 )); then
        mapfile -t COMPREPLY < <(compgen -W "$commands --help --install-completion --show-completion" -- "$cur")
        return
    fi

    case "$command" in
        start|add) options="-t --time -v --verbose --help" ;;
        report) options="-d --day -f --from -t --to --help" ;;
        export) options="-d --day -f --from -t --to --format -o --output -v --verbose --help" ;;
        submit) options="-d --day -f --from -t --to -n --dry-run --headed -i --interactive -r --resume --sleep -v --verbose --help" ;;
        login|weeklyeditor) options="-v --verbose --help" ;;
        tutorial|whats-new|help) options="--help" ;;
        config)
            if (( COMP_CWORD == 2 )); then
                mapfile -t COMPREPLY < <(compgen -W "$config_commands --help" -- "$cur")
                return
            fi
            case "${COMP_WORDS[2]}" in
                show|edit|path|set|get|wizard) options="-v --verbose --help" ;;
                *) return ;;
            esac
            ;;
        *) return ;;
    esac

    if [[ "$command" == export && "$prev" == = && "${COMP_WORDS[COMP_CWORD-2]:-}" == --format ]]; then
        mapfile -t COMPREPLY < <(compgen -W "json csv" -- "$cur")
        return
    fi
    if [[ ( "$command" == start || "$command" == add ) && "$prev" == = && "${COMP_WORDS[COMP_CWORD-2]:-}" == --time ]]; then
        return
    fi

    case "$command:$prev" in
        export:--format)
            mapfile -t COMPREPLY < <(compgen -W "json csv" -- "$cur")
            return
            ;;
        export:-o|export:--output)
            mapfile -t COMPREPLY < <(compgen -f -- "$cur")
            compopt -o filenames 2>/dev/null || true
            return
            ;;
        report:-d|report:--day|report:-f|report:--from|report:-t|report:--to|\
        submit:-d|submit:--day|submit:-f|submit:--from|submit:-t|submit:--to|\
        export:-d|export:--day)
            mapfile -t COMPREPLY < <(compgen -W "$dates" -- "$cur")
            return
            ;;
        start:-t|start:--time|add:-t|add:--time|export:-f|export:--from|\
        export:-t|export:--to|submit:--sleep)
            return
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        mapfile -t COMPREPLY < <(compgen -W "$options" -- "$cur")
        return
    fi

    case "$command" in
        help)
            if (( COMP_CWORD == 2 )); then
                mapfile -t COMPREPLY < <(compgen -W "$commands" -- "$cur")
            elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[2]}" == config ]]; then
                mapfile -t COMPREPLY < <(compgen -W "$config_commands" -- "$cur")
            fi
            return
            ;;
        config)
            # Config keys are free-form; only suggest keys already in the file.
            if [[ "${COMP_WORDS[2]}" != get && "${COMP_WORDS[2]}" != set ]]; then
                return
            fi
            local word
            for word in "${COMP_WORDS[@]:3:COMP_CWORD-3}"; do
                [[ "$word" == -v || "$word" == --verbose ]] || return 0
            done
            ;;
        add) ;;
        *) return ;;
    esac

    local snap_dir="$HOME/snap/punch/current"
    local config_file tasks_file
    if [[ -n "${PUNCH_CONFIG_DIR:-}" ]]; then
        config_file="$PUNCH_CONFIG_DIR/punch.yaml"
    elif [[ -f "$snap_dir/.config/punch/punch.yaml" ]]; then
        config_file="$snap_dir/.config/punch/punch.yaml"
    else
        config_file="${XDG_CONFIG_HOME:-$HOME/.config}/punch/punch.yaml"
    fi
    if [[ -n "${PUNCH_DATA_DIR:-}" ]]; then
        tasks_file="$PUNCH_DATA_DIR/tasks.txt"
    elif [[ -f "$snap_dir/.local/share/punch/tasks.txt" ]]; then
        tasks_file="$snap_dir/.local/share/punch/tasks.txt"
    else
        tasks_file="${XDG_DATA_HOME:-$HOME/.local/share}/punch/tasks.txt"
    fi

    if [[ "$command" == config ]]; then
        if [[ -r "$config_file" ]]; then
            mapfile -t COMPREPLY < <(compgen -W "$(awk '/^[A-Za-z_][A-Za-z0-9_-]*:/ {sub(/:.*/, ""); print}' "$config_file")" -- "$cur")
        fi
        return
    fi

    local shorts="" catname="" line code i
    local -A short_to_full=()
    if [[ -r "$config_file" ]]; then
        while IFS= read -r line; do
            if [[ "$line" =~ ^[[:space:]]{2}([A-Za-z0-9\ \-_]+):[[:space:]]*$ ]]; then
                catname="${BASH_REMATCH[1]}"
            elif [[ "$line" =~ short:[[:space:]]*([A-Za-z0-9_]+) ]]; then
                code="${BASH_REMATCH[1]}"
                shorts+=" $code"
                short_to_full["$code"]="$catname"
            fi
        done < "$config_file"
    fi

    local -a args=()
    for ((i=2; i<COMP_CWORD; i++)); do
        case "${COMP_WORDS[i]}" in
            -t|--time)
                ((i+=1))
                if [[ "${COMP_WORDS[i]:-}" == = ]]; then
                    ((i+=1))
                fi
                if [[ "${COMP_WORDS[i+1]:-}" == : && "${COMP_WORDS[i+2]:-}" =~ ^[0-9]+$ ]]; then
                    ((i+=2))
                fi
                ;;
            --time=*) ;;
            -v|--verbose) ;;
            *) args+=("${COMP_WORDS[i]}") ;;
        esac
    done
    if (( ${#args[@]} == 0 )); then
        mapfile -t COMPREPLY < <(compgen -W "$shorts" -- "$cur")
        return
    fi

    local short="${args[0]%:}"
    [[ -n "$short" ]] || return 0
    if (( ${#args[@]} == 1 )) && [[ "${args[0]}" != *: ]]; then
        mapfile -t COMPREPLY < <(compgen -W ":" -- "$cur")
        return
    fi
    if (( ${#args[@]} > 2 )) || { (( ${#args[@]} == 2 )) && [[ "${args[1]}" != : ]]; }; then
        return
    fi
    local fullcat="${short_to_full[$short]:-}" task
    if [[ -n "$fullcat" && -r "$tasks_file" ]]; then
        while IFS= read -r task; do
            if [[ "$task" == "$cur"* ]]; then
                COMPREPLY+=("$task")
            fi
        done < <(awk -F'|' -v cat="$fullcat" '
            { gsub(/^[ \t]+|[ \t]+$/, "", $2);
              gsub(/^[ \t]+|[ \t]+$/, "", $3); }
            $2 == cat && $3 != "" { print $3 }
        ' "$tasks_file" | sort -u)
        compopt -o filenames 2>/dev/null || true
    fi
    return 0
}

# Keep ":" in COMP_WORDBREAKS for the existing task-entry syntax.
complete -F _punch_complete punch.py punch
