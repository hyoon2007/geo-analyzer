import json
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

from bs4 import BeautifulSoup


# Allowed structural audit actions
ALLOWED_ACTIONS = {
    'change_tag',
    # Legacy actions (kept for backward compatibility)
    'change_tag_to_h1',
    'change_tag_to_h2',
    'change_tag_to_h3',
    'change_tag_to_article',
    'change_tag_to_section',
    'update_text',
}

# Tag mapping for legacy change_tag_to_* actions
LEGACY_TAG_MAP = {
    'change_tag_to_h1': 'h1',
    'change_tag_to_h2': 'h2',
    'change_tag_to_h3': 'h3',
    'change_tag_to_article': 'article',
    'change_tag_to_section': 'section',
}

# Expandable allow-list for change_tag target_tag
ALLOWED_TARGET_TAGS = {
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'article',
    'section',
    'main',
    'nav',
    'aside',
    'header',
    'footer',
    'p',
    'ul',
    'ol',
    'li',
}


def resolve_structural_action(rec: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """
    Resolve action into normalized executable form.

    Returns:
        (normalized_action, target_tag, error_reason)
    """
    action = rec.get('action')

    if action == 'update_text':
        return 'update_text', None, None

    if action == 'change_tag':
        target_tag = rec.get('target_tag')
        if not isinstance(target_tag, str) or not target_tag.strip():
            return None, None, 'change_tag: target_tag is missing or empty'
        target_tag = target_tag.strip().lower()
        if target_tag not in ALLOWED_TARGET_TAGS:
            return None, None, f"change_tag: unsupported target_tag '{target_tag}'"
        return 'change_tag', target_tag, None

    if action in LEGACY_TAG_MAP:
        return 'change_tag', LEGACY_TAG_MAP[action], None

    return None, None, f"Invalid action '{action}'"


def apply_structural_audit_injector(
    html: str,
    structural_audit: dict[str, Any],
    debug: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Apply structural audit recommendations to HTML.
    
    Returns:
        (modified_html, report_dict)
    """
    report = {
        'status': 'passed',
        'applied': 0,
        'skipped': 0,
        'failed': 0,
        'items': [],
    }

    if not isinstance(structural_audit, dict):
        report['status'] = 'failed'
        report['items'].append({'reason': 'structural_audit is not a dict'})
        return html, report

    recommendations = structural_audit.get('recommendations', [])
    if not isinstance(recommendations, list):
        report['status'] = 'failed'
        report['items'].append({'reason': 'recommendations is not a list'})
        return html, report

    try:
        soup = BeautifulSoup(html, 'html.parser')
    except (TypeError, ValueError, AttributeError) as e:
        report['status'] = 'failed'
        report['items'].append({'reason': f'BeautifulSoup parse error: {e}'})
        return html, report

    for idx, rec in enumerate(recommendations):
        if not isinstance(rec, dict):
            report['skipped'] += 1
            report['items'].append(
                {
                    'index': idx,
                    'selector': None,
                    'action': None,
                    'status': 'skipped',
                    'reason': 'recommendation item is not an object',
                },
            )
            continue

        item_report = {
            'index': idx,
            'selector': rec.get('selector'),
            'action': rec.get('action'),
            'status': 'pending',
            'reason': '',
        }

        # Validation: action
        action = rec.get('action')
        if action not in ALLOWED_ACTIONS:
            item_report['status'] = 'skipped'
            item_report['reason'] = f"Invalid action '{action}'"
            report['skipped'] += 1
            report['items'].append(item_report)
            continue

        normalized_action, target_tag, action_error = resolve_structural_action(rec)
        if action_error:
            item_report['status'] = 'skipped'
            item_report['reason'] = action_error
            report['skipped'] += 1
            report['items'].append(item_report)
            continue
        item_report['normalized_action'] = normalized_action

        # Validation: selector
        selector = rec.get('selector')
        if not isinstance(selector, str) or not selector.strip():
            item_report['status'] = 'skipped'
            item_report['reason'] = 'selector is empty or not a string'
            report['skipped'] += 1
            report['items'].append(item_report)
            continue

        # Find target nodes
        try:
            nodes = soup.select(selector)
        except (TypeError, ValueError, AttributeError) as e:
            item_report['status'] = 'skipped'
            item_report['reason'] = f'CSS selector error: {e}'
            report['skipped'] += 1
            report['items'].append(item_report)
            continue

        # Validate uniqueness
        if len(nodes) == 0:
            item_report['status'] = 'skipped'
            item_report['reason'] = 'selector matched 0 nodes'
            report['skipped'] += 1
            report['items'].append(item_report)
            continue
        elif len(nodes) > 1:
            item_report['status'] = 'skipped'
            item_report['reason'] = f'selector matched {len(nodes)} nodes (not unique)'
            report['skipped'] += 1
            report['items'].append(item_report)
            continue

        node = nodes[0]

        # Apply action
        try:
            if normalized_action == 'update_text':
                new_text = rec.get('new_text')
                if not isinstance(new_text, str) or not new_text.strip():
                    item_report['status'] = 'skipped'
                    item_report['reason'] = 'update_text: new_text is missing or empty'
                    report['skipped'] += 1
                    report['items'].append(item_report)
                    continue

                old_text = node.get_text(strip=False)
                node.string = new_text
                item_report['status'] = 'applied'
                item_report['old_text'] = old_text[:100]
                item_report['new_text'] = new_text[:100]

            elif normalized_action == 'change_tag':
                if target_tag is None:
                    item_report['status'] = 'skipped'
                    item_report['reason'] = 'change_tag: target_tag resolution failed'
                    report['skipped'] += 1
                    report['items'].append(item_report)
                    continue

                new_tag = target_tag
                old_tag = node.name
                node.name = new_tag
                item_report['status'] = 'applied'
                item_report['old_tag'] = old_tag
                item_report['new_tag'] = new_tag

            report['applied'] += 1
            report['items'].append(item_report)

        except (TypeError, ValueError, AttributeError) as e:
            item_report['status'] = 'failed'
            item_report['reason'] = f'Action execution error: {e}'
            report['failed'] += 1
            report['items'].append(item_report)

    if report['applied'] > 0:
        report['status'] = 'applied'
    elif report['skipped'] > 0 and report['failed'] == 0:
        report['status'] = 'skipped'
    elif report['failed'] > 0:
        report['status'] = 'failed'

    if debug and report['items']:
        print('[Debug] Structural Audit Injector changes:')
        for item in report['items']:
            print(f"  - {item.get('selector', '?')} {item['action']}: {item['status']}")

    return str(soup), report


def apply_meta_tag_injector(
    html: str,
    enriched_meta: dict[str, Any],
    debug: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Upsert title/description/og meta tags into the document head.
    Supports both nested og_tags and flat og:* keys.
    
    Returns:
        (modified_html, report_dict)
    """
    report = {
        'status': 'passed',
        'items': [],
    }

    try:
        soup = BeautifulSoup(html, 'html.parser')
        head = soup.find('head')
        if head is None:
            report['status'] = 'failed'
            report['items'].append({'reason': 'No <head> tag found'})
            return html, report

        logs: list[str] = []

        # Apply title
        title = enriched_meta.get('title')
        if isinstance(title, str) and title.strip():
            title_tag = head.find('title')
            if title_tag:
                before = title_tag.get_text(strip=False)
                title_tag.string = title
                logs.append(f"title: '{before}' -> '{title}'")
            else:
                new_title = soup.new_tag('title')
                new_title.string = title
                head.append(new_title)
                logs.append(f"title: added '{title}'")

        # Apply description
        description = enriched_meta.get('description')
        if isinstance(description, str) and description.strip():
            desc_tag = head.find('meta', attrs={'name': 'description'})
            if desc_tag:
                before = desc_tag.get('content', '')
                desc_tag['content'] = description
                logs.append(f"meta description: '{before}' -> '{description}'")
            else:
                head.append(
                    soup.new_tag(
                        'meta',
                        attrs={'name': 'description', 'content': description},
                    ),
                )
                logs.append(f"meta description: added '{description}'")

        # Apply og_tags (nested structure priority, then flat keys as fallback)
        og_tags_dict = enriched_meta.get('og_tags', {})
        if isinstance(og_tags_dict, dict):
            og_sources = og_tags_dict
        else:
            og_sources = {}

        # Fallback: include flat og:* keys from enriched_meta if not in og_tags
        for key in enriched_meta:
            if key.startswith('og:') and key not in og_sources:
                og_sources[key] = enriched_meta[key]

        for key, value in og_sources.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if not key.startswith('og:'):
                key = f'og:{key}'
            
            og_tag = head.find('meta', attrs={'property': key})
            if og_tag:
                before = og_tag.get('content', '')
                og_tag['content'] = value
                logs.append(f"{key}: '{before}' -> '{value}'")
            else:
                head.append(
                    soup.new_tag(
                        'meta',
                        attrs={'property': key, 'content': value},
                    ),
                )
                logs.append(f"{key}: added '{value}'")

        if logs:
            report['status'] = 'applied'
            report['items'] = logs

        if debug and logs:
            print('[Debug] Meta Tag Injector changes:')
            for line in logs:
                print(f'  - {line}')

        return str(soup), report

    except (AttributeError, TypeError, ValueError) as e:
        report['status'] = 'failed'
        report['items'].append({'reason': str(e)})
        print(f"[Error] Meta tag injection failed: {e}")
        return html, report


def apply_json_ld_manager(
    html: str,
    json_ld: dict[str, Any],
    debug: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Replace same-@type JSON-LD and append the new JSON-LD before </body>.
    
    Returns:
        (modified_html, report_dict)
    """
    report = {
        'status': 'passed',
        'items': [],
    }

    if '@type' not in json_ld:
        report['status'] = 'skipped'
        report['items'].append({'reason': 'json_ld has no @type'})
        return html, report

    try:
        soup = BeautifulSoup(html, 'html.parser')
        target_type = json_ld.get('@type')
        logs: list[str] = []

        for tag in soup.find_all('script', attrs={'type': 'application/ld+json'}):
            raw = tag.string or ''
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get('@type') == target_type:
                tag.decompose()
                logs.append(f"removed existing json-ld @type='{target_type}'")

        new_tag = soup.new_tag('script', attrs={'type': 'application/ld+json'})
        new_tag.string = json.dumps(json_ld, ensure_ascii=False, indent=2)

        body = soup.find('body')
        if body:
            body.append(new_tag)
        else:
            soup.append(new_tag)
        logs.append(f"added json-ld @type='{target_type}'")

        report['status'] = 'applied'
        report['items'] = logs

        if debug:
            print('[Debug] JSON-LD Manager changes:')
            for line in logs:
                print(f'  - {line}')

        return str(soup), report

    except (AttributeError, TypeError, ValueError) as e:
        report['status'] = 'failed'
        report['items'].append({'reason': str(e)})
        print(f"[Error] JSON-LD injection failed: {e}")
        return html, report


def apply_rag_optimization_injector(
    html: str,
    rag_optimization: dict[str, Any],
    *,
    enabled: bool,
    target: str,
    max_chars: int,
    debug: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Inject rag_optimization.tldr_passage into a visible area of the document.

    Returns:
        (modified_html, report_dict)
    """
    report = {
        'status': 'passed',
        'items': [],
    }

    if not enabled:
        report['status'] = 'skipped'
        report['items'].append({'reason': 'rag injection disabled by config'})
        return html, report

    if not isinstance(rag_optimization, dict):
        report['status'] = 'skipped'
        report['items'].append({'reason': 'rag_optimization is not a dict'})
        return html, report

    tldr_passage = rag_optimization.get('tldr_passage')
    if not isinstance(tldr_passage, str) or not tldr_passage.strip():
        report['status'] = 'skipped'
        report['items'].append({'reason': 'rag_optimization.tldr_passage is missing or empty'})
        return html, report

    # Keep passage size bounded for stable rendering and indexing behavior.
    normalized_passage = tldr_passage.strip()
    if max_chars > 0 and len(normalized_passage) > max_chars:
        normalized_passage = normalized_passage[:max_chars]
        report['items'].append({'warning': f'tldr_passage truncated to {max_chars} chars'})

    try:
        soup = BeautifulSoup(html, 'html.parser')

        body = soup.find('body')
        if body is None:
            report['status'] = 'failed'
            report['items'].append({'reason': 'No <body> tag found'})
            return html, report

        target_key = (target or 'main').strip().lower()
        if target_key == 'main':
            anchor = body.find('main') or body.find('article') or body
        elif target_key == 'article':
            anchor = body.find('article') or body.find('main') or body
        else:
            anchor = body

        existing_block = soup.find('section', attrs={'data-geo-generated': 'rag_tldr'})
        if existing_block is not None:
            text_node = existing_block.find('p')
            if text_node is None:
                text_node = soup.new_tag('p')
                existing_block.append(text_node)
            old_text = text_node.get_text(strip=False)
            if old_text == normalized_passage:
                report['status'] = 'skipped'
                report['items'].append({'reason': 'rag tldr no-op (same text already injected)'})
                return str(soup), report

            text_node.string = normalized_passage
            report['status'] = 'applied'
            report['items'].append('updated existing visible rag tldr block')
            return str(soup), report

        rag_section = soup.new_tag(
            'section',
            attrs={
                'class': 'geo-tldr',
                'data-geo-generated': 'rag_tldr',
            },
        )
        rag_title = soup.new_tag('h2')
        rag_title.string = 'Quick Summary'
        rag_paragraph = soup.new_tag('p')
        rag_paragraph.string = normalized_passage
        rag_section.append(rag_title)
        rag_section.append(rag_paragraph)

        anchor.insert(0, rag_section)
        report['status'] = 'applied'
        report['items'].append(f"inserted visible rag tldr block under <{anchor.name}>")

        if debug:
            print('[Debug] RAG Optimization Injector changes:')
            for line in report['items']:
                print(f'  - {line}')

        return str(soup), report

    except (AttributeError, TypeError, ValueError) as e:
        report['status'] = 'failed'
        report['items'].append({'reason': str(e)})
        print(f"[Error] RAG optimization injection failed: {e}")
        return html, report


def inject_llm_results_to_html(
    source_url: str,
    preprocessed_html: str,
    geo_result_json: dict[str, Any],
    rag_injection_enabled: bool = False,
    rag_injection_target: str = 'main',
    rag_tldr_max_chars: int = 700,
    debug: bool = False,
) -> tuple[str, dict[str, Any]]:
    """
    Build enriched HTML by applying GEO result fields to preprocessed HTML.
    Execution order: structural_audit → enriched_meta → json_ld → rag_optimization
    
    Returns:
        (enriched_html, injection_report)
    """
    enriched_html = preprocessed_html
    injection_report = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source_url': source_url,
        'structural_audit': {},
        'enriched_meta': {},
        'json_ld': {},
        'rag_optimization': {},
    }

    # Step 1: Apply structural audit
    structural_audit = geo_result_json.get('structural_audit')
    if isinstance(structural_audit, dict):
        enriched_html, audit_report = apply_structural_audit_injector(
            enriched_html, structural_audit, debug
        )
        injection_report['structural_audit'] = audit_report

    # Step 2: Apply enriched meta (title, description, og_tags)
    enriched_meta = geo_result_json.get('enriched_meta')
    if isinstance(enriched_meta, dict):
        enriched_html, meta_report = apply_meta_tag_injector(
            enriched_html, enriched_meta, debug
        )
        injection_report['enriched_meta'] = meta_report

    # Step 3: Apply JSON-LD
    json_ld = geo_result_json.get('json_ld')
    if isinstance(json_ld, dict):
        enriched_html, json_ld_report = apply_json_ld_manager(
            enriched_html, json_ld, debug
        )
        injection_report['json_ld'] = json_ld_report

    # Step 4: Apply RAG optimization visible passage
    rag_optimization = geo_result_json.get('rag_optimization')
    if not isinstance(rag_optimization, dict):
        # Backward compatibility for older prompt schema.
        legacy_rag = geo_result_json.get('rag_optimized_passage')
        if isinstance(legacy_rag, dict):
            rag_optimization = {
                'tldr_passage': legacy_rag.get('tldr_passage') or legacy_rag.get('tldr_chunk', ''),
            }

    if isinstance(rag_optimization, dict):
        enriched_html, rag_report = apply_rag_optimization_injector(
            enriched_html,
            rag_optimization,
            enabled=rag_injection_enabled,
            target=rag_injection_target,
            max_chars=rag_tldr_max_chars,
            debug=debug,
        )
        injection_report['rag_optimization'] = rag_report
    else:
        injection_report['rag_optimization'] = {
            'status': 'skipped',
            'items': [{'reason': 'rag_optimization field not found'}],
        }

    if debug:
        print(f"[Debug] HTML injection completed for {source_url}")

    return enriched_html, injection_report


def save_injection_report(
    report: dict[str, Any],
    output_dir: Path,
    domain: str = '',
    timestamp: str = '',
) -> Path | None:
    """
    Save injection report to a JSON file.
    
    Args:
        report: Injection report dict
        output_dir: Directory to save report (will be created if not exists)
        domain: Domain name for filename (e.g., 'samsung_com')
        timestamp: Timestamp string for filename (e.g., '20260511_150607')
    
    Returns:
        Path to saved report, or None if failed
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f"injection_report_{domain}_{timestamp}.json"
        file_path = output_dir / filename
        
        file_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return file_path
    except (OSError, TypeError) as e:
        print(f"[Error] Failed to save injection report: {e}")
        return None
