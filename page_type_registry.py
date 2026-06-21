import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.properties'
DEFAULT_REGISTRY_PATH = BASE_DIR / 'customer_page_types.json'


def load_properties(config_path: Path = CONFIG_PATH) -> dict[str, str]:
    if not config_path.exists():
        return {}

    properties: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        properties[key.strip()] = value.strip()
    return properties


def get_setting(
    properties: dict[str, str],
    env_key: str,
    property_key: str,
    default: str,
) -> str:
    value = os.getenv(env_key)
    if value is not None and value.strip():
        return value.strip()
    return properties.get(property_key, default).strip()


@dataclass(frozen=True)
class PageTypeMatch:
    customer_id: str
    page_type: str
    ttl_seconds: int


@dataclass(frozen=True)
class PageTypeSettings:
    customer_id: str
    registry_path: Path

    @classmethod
    def from_env_and_properties(cls) -> 'PageTypeSettings':
        properties = load_properties()
        return cls(
            customer_id=get_setting(
                properties,
                'GEO_CUSTOMER_ID',
                'customer.id',
                '',
            ),
            registry_path=Path(
                get_setting(
                    properties,
                    'GEO_CUSTOMER_PAGE_TYPES_PATH',
                    'customer.page_types_path',
                    str(DEFAULT_REGISTRY_PATH),
                )
            ).expanduser(),
        )


def build_match_targets(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f'Invalid URL for page type matching: {url}')

    path = parsed.path or '/'
    if parsed.query:
        path = f'{path}?{parsed.query}'
    return f'{parsed.netloc}{path}', path


def load_registry(path: Path) -> dict:
    if not path.is_absolute():
        path = BASE_DIR / path
    return json.loads(path.read_text(encoding='utf-8'))


def resolve_page_type(
    url: str,
    settings: PageTypeSettings | None = None,
) -> PageTypeMatch:
    effective_settings = settings or PageTypeSettings.from_env_and_properties()
    registry = load_registry(effective_settings.registry_path)

    customer_id = effective_settings.customer_id or registry.get('default_customer_id')
    if not customer_id:
        raise ValueError('customer.id is empty and registry has no default_customer_id')

    customers = registry.get('customers') or {}
    customer = customers.get(customer_id)
    if customer is None:
        raise ValueError(f"Missing customer page type definition: {customer_id}")

    key_source, path_query = build_match_targets(url)
    for page_type in customer.get('page_types') or []:
        ttl_seconds = int(page_type['ttl_seconds'])
        for pattern in page_type.get('patterns') or []:
            if fnmatch.fnmatch(key_source, pattern) or fnmatch.fnmatch(path_query, pattern):
                return PageTypeMatch(
                    customer_id=customer_id,
                    page_type=page_type['name'],
                    ttl_seconds=ttl_seconds,
                )

    return PageTypeMatch(
        customer_id=customer_id,
        page_type=customer.get('default_page_type', 'default'),
        ttl_seconds=int(customer.get('default_ttl_seconds', 86400)),
    )
