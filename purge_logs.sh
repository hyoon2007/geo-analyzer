#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
DELETE_ALL=false
CONFIRM=false
DAYS=""
TYPE=""
CONFIG_PATH="./config.properties"

usage() {
  cat <<'EOF'
Purge log files from geo-analyzer output directories

Usage:
  ./purge_logs.sh --dry-run [--days N] [--type rendered_html|preprocessor|llm_prompt|llm_response|llm_raw_response|llm_repair|enriched_html|injection_report]
  ./purge_logs.sh --all [--days N] [--type rendered_html|preprocessor|llm_prompt|llm_response|llm_raw_response|llm_repair|enriched_html|injection_report] [--confirm]

Options:
  --dry-run         Show files that would be deleted without deleting
  --days N          Only target files older than N days
  --type TYPE       One of: rendered_html, preprocessor, llm_prompt, llm_response, llm_raw_response, llm_repair, enriched_html, injection_report
  --all             Delete all matching files (required unless --dry-run)
  --confirm         Skip confirmation prompt before deletion
  --config PATH     Path to config.properties (default: ./config.properties)
  --help            Show this help message

Examples:
  ./purge_logs.sh --dry-run --days 7
  ./purge_logs.sh --all --confirm
  ./purge_logs.sh --type llm_prompt --days 3 --all --confirm
  ./purge_logs.sh --type llm_raw_response --all --confirm
  ./purge_logs.sh --type llm_repair --all --confirm
  ./purge_logs.sh --type rendered_html --all --confirm
  ./purge_logs.sh --type enriched_html --all --confirm
  ./purge_logs.sh --type injection_report --all --confirm
EOF
}

get_property() {
  local key="$1"
  awk -v target_key="$key" '
    /^[[:space:]]*#/ { next }
    index($0, "=") == 0 { next }
    {
      key_part = $0
      sub(/=.*/, "", key_part)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key_part)
      if (key_part == target_key) {
        value_part = $0
        sub(/^[^=]*=/, "", value_part)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value_part)
        print value_part
        exit
      }
    }
  ' "$CONFIG_PATH"
}

require_property() {
  local key="$1"
  local value
  value="$(get_property "$key")"
  if [[ -z "$value" ]]; then
    echo "Error: Required property '$key' not found in $CONFIG_PATH" >&2
    exit 1
  fi
  printf '%s\n' "$value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --days)
      if [[ $# -lt 2 ]]; then
        echo "Error: --days requires a numeric value" >&2
        exit 1
      fi
      DAYS="$2"
      if ! [[ "$DAYS" =~ ^[0-9]+$ ]]; then
        echo "Error: --days must be a non-negative integer" >&2
        exit 1
      fi
      shift 2
      ;;
    --type)
      if [[ $# -lt 2 ]]; then
        echo "Error: --type requires a value" >&2
        exit 1
      fi
      TYPE="$2"
      case "$TYPE" in
        rendered_html|preprocessor|llm_prompt|llm_response|llm_raw_response|llm_repair|enriched_html|injection_report)
          ;;
        *)
          echo "Error: --type must be one of rendered_html, preprocessor, llm_prompt, llm_response, llm_raw_response, llm_repair, enriched_html, injection_report" >&2
          exit 1
          ;;
      esac
      shift 2
      ;;
    --all)
      DELETE_ALL=true
      shift
      ;;
    --confirm)
      CONFIRM=true
      shift
      ;;
    --config)
      if [[ $# -lt 2 ]]; then
        echo "Error: --config requires a path" >&2
        exit 1
      fi
      CONFIG_PATH="$2"
      shift 2
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Error: Unknown option '$1'" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$DRY_RUN" == false && "$DELETE_ALL" == false ]]; then
  echo "Error: Either --dry-run or --all must be specified" >&2
  echo "Use --help for more information" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Error: Config file not found at $CONFIG_PATH" >&2
  exit 1
fi

BASE_DIR="$(require_property "output.base_dir")"
RENDERED_HTML_SUBDIR="$(get_property "output.rendered_html_subdir")"
PREPROCESSOR_SUBDIR="$(require_property "output.preprocessor_subdir")"
LLM_PROMPT_SUBDIR="$(require_property "output.llm_prompt_subdir")"
LLM_RESPONSE_SUBDIR="$(require_property "output.llm_subdir")"
LLM_RAW_RESPONSE_SUBDIR="$(get_property "output.llm_raw_response_subdir")"
LLM_REPAIR_SUBDIR="$(get_property "output.llm_repair_subdir")"
ENRICHED_HTML_SUBDIR="$(get_property "output.enriched_html_subdir")"
INJECTION_REPORT_SUBDIR="$(get_property "output.injection_report_subdir")"

if [[ -z "$RENDERED_HTML_SUBDIR" ]]; then
  RENDERED_HTML_SUBDIR="rendered_html"
fi

if [[ -z "$ENRICHED_HTML_SUBDIR" ]]; then
  ENRICHED_HTML_SUBDIR="enriched_html"
fi

if [[ -z "$LLM_RAW_RESPONSE_SUBDIR" ]]; then
  LLM_RAW_RESPONSE_SUBDIR="llm_raw_response"
fi

if [[ -z "$LLM_REPAIR_SUBDIR" ]]; then
  LLM_REPAIR_SUBDIR="llm_repair"
fi

if [[ -z "$INJECTION_REPORT_SUBDIR" ]]; then
  INJECTION_REPORT_SUBDIR="injection_report"
fi

RENDERED_HTML_DIR="$BASE_DIR/$RENDERED_HTML_SUBDIR"
PREPROCESSOR_DIR="$BASE_DIR/$PREPROCESSOR_SUBDIR"
LLM_PROMPT_DIR="$BASE_DIR/$LLM_PROMPT_SUBDIR"
LLM_RESPONSE_DIR="$BASE_DIR/$LLM_RESPONSE_SUBDIR"
LLM_RAW_RESPONSE_DIR="$BASE_DIR/$LLM_RAW_RESPONSE_SUBDIR"
LLM_REPAIR_DIR="$BASE_DIR/$LLM_REPAIR_SUBDIR"
ENRICHED_HTML_DIR="$BASE_DIR/$ENRICHED_HTML_SUBDIR"
INJECTION_REPORT_DIR="$BASE_DIR/$INJECTION_REPORT_SUBDIR"

if [[ ! -d "$BASE_DIR" ]]; then
  echo "Error: Base directory not found at $BASE_DIR" >&2
  exit 1
fi

echo
echo "Geo-Analyzer Log Purge Utility"
echo "Base directory: $BASE_DIR"

declare -a TARGET_LABELS=()
declare -a TARGET_DIRS=()

add_target() {
  TARGET_LABELS+=("$1")
  TARGET_DIRS+=("$2")
}

if [[ -n "$TYPE" ]]; then
  case "$TYPE" in
    rendered_html)
      add_target "rendered_html" "$RENDERED_HTML_DIR"
      ;;
    preprocessor)
      add_target "preprocessor" "$PREPROCESSOR_DIR"
      ;;
    llm_prompt)
      add_target "llm_prompt" "$LLM_PROMPT_DIR"
      ;;
    llm_response)
      add_target "llm_response" "$LLM_RESPONSE_DIR"
      ;;
    llm_raw_response)
      add_target "llm_raw_response" "$LLM_RAW_RESPONSE_DIR"
      ;;
    llm_repair)
      add_target "llm_repair" "$LLM_REPAIR_DIR"
      ;;
    enriched_html)
      add_target "enriched_html" "$ENRICHED_HTML_DIR"
      ;;
    injection_report)
      add_target "injection_report" "$INJECTION_REPORT_DIR"
      ;;
  esac
else
  add_target "rendered_html" "$RENDERED_HTML_DIR"
  add_target "preprocessor" "$PREPROCESSOR_DIR"
  add_target "llm_prompt" "$LLM_PROMPT_DIR"
  add_target "llm_response" "$LLM_RESPONSE_DIR"
  add_target "llm_raw_response" "$LLM_RAW_RESPONSE_DIR"
  add_target "llm_repair" "$LLM_REPAIR_DIR"
  add_target "enriched_html" "$ENRICHED_HTML_DIR"
  add_target "injection_report" "$INJECTION_REPORT_DIR"
fi

if [[ -n "$DAYS" ]]; then
  echo "Filtering files older than $DAYS day(s)..."
fi

declare -i total_count=0
declare -i total_size=0
declare -a all_labels=()
declare -a all_files=()

echo
echo "Files to be $([[ "$DELETE_ALL" == true ]] && echo "DELETED" || echo "shown") (dry-run mode):"

for i in "${!TARGET_LABELS[@]}"; do
  label="${TARGET_LABELS[$i]}"
  dir="${TARGET_DIRS[$i]}"
  declare -a items=()

  if [[ -d "$dir" ]]; then
    if [[ -n "$DAYS" ]]; then
      while IFS= read -r file; do
        [[ -z "$file" ]] && continue
        items+=("$file")
      done < <(find "$dir" -type f -mtime "+$DAYS" | sort)
    else
      while IFS= read -r file; do
        [[ -z "$file" ]] && continue
        items+=("$file")
      done < <(find "$dir" -type f | sort)
    fi
  fi

  if [[ ${#items[@]} -eq 0 ]]; then
    continue
  fi

  echo
  echo "  $label/: ${#items[@]} file(s)"

  for file in "${items[@]}"; do
    [[ -z "$file" ]] && continue
    size=$(stat -f '%z' "$file" 2>/dev/null || echo 0)
    mtime=$(stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S' "$file" 2>/dev/null || echo "unknown")
    name="$(basename "$file")"

    if (( size > 1024 )); then
      size_str=$(awk -v s="$size" 'BEGIN { printf "%.1fKB", s/1024 }')
    else
      size_str="${size}B"
    fi

    echo "     - ${name} (${size_str}, ${mtime})"

    total_count=$((total_count + 1))
    total_size=$((total_size + size))
    all_labels+=("$label")
    all_files+=("$file")
  done
done

if (( total_count == 0 )); then
  echo
  echo "No files found matching criteria"
  exit 0
fi

total_mb=$(awk -v s="$total_size" 'BEGIN { printf "%.2f", s/1024/1024 }')
echo
echo "  Total: ${total_count} file(s), ${total_mb}MB"

if [[ "$DRY_RUN" == true ]]; then
  echo
  echo "Dry-run complete. No files were deleted."
  exit 0
fi

if [[ "$CONFIRM" == false ]]; then
  echo
  echo "WARNING: This will permanently delete the files listed above!"
  read -r -p "Type 'yes' to confirm deletion: " answer
  if [[ "$answer" != "yes" ]]; then
    echo "Cancelled."
    exit 0
  fi
fi

echo
echo "Deleting files..."
declare -i deleted_count=0

for i in "${!all_files[@]}"; do
  label="${all_labels[$i]}"
  file="${all_files[$i]}"
  if rm -f "$file"; then
    echo "  Deleted: ${label}/$(basename "$file")"
    deleted_count=$((deleted_count + 1))
  else
    echo "  Failed to delete: ${label}/$(basename "$file")" >&2
  fi
done

echo
echo "Successfully deleted ${deleted_count}/${total_count} file(s)"
if (( deleted_count < total_count )); then
  echo "Warning: $((total_count - deleted_count)) file(s) failed to delete" >&2
fi
