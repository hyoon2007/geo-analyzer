import base64
import configparser
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from page_type_registry import resolve_page_type


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config.properties'
DEFAULT_EDGEKV_API_PATH_TEMPLATE = (
    '/edgekv/v1/networks/{network}/namespaces/{namespace}/groups/{group_id}/items/{item_id}'
)


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


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class EdgeKVSettings:
    enabled: bool
    namespace: str
    group_id: str
    network: str
    edgerc_path: Path
    edgerc_section: str
    timeout_seconds: int
    data_version: str

    @classmethod
    def from_env_and_properties(cls) -> 'EdgeKVSettings':
        properties = load_properties()
        return cls(
            enabled=parse_bool(
                get_setting(properties, 'GEO_EDGEKV_ENABLED', 'edgekv.enabled', 'false')
            ),
            namespace=get_setting(
                properties,
                'GEO_EDGEKV_NAMESPACE',
                'edgekv.namespace',
                'geo_opt_data',
            ),
            group_id=get_setting(
                properties,
                'GEO_EDGEKV_GROUP_ID',
                'edgekv.group_id',
                'url_metadata',
            ),
            network=get_setting(
                properties,
                'GEO_EDGEKV_NETWORK',
                'edgekv.network',
                'production',
            ),
            edgerc_path=Path(
                get_setting(
                    properties,
                    'GEO_EDGEKV_EDGERC_PATH',
                    'edgekv.edgerc_path',
                    '~/.edgerc',
                )
            ).expanduser(),
            edgerc_section=get_setting(
                properties,
                'GEO_EDGEKV_EDGERC_SECTION',
                'edgekv.edgerc_section',
                'default',
            ),
            timeout_seconds=int(
                get_setting(
                    properties,
                    'GEO_EDGEKV_TIMEOUT_SECONDS',
                    'edgekv.timeout_seconds',
                    '30',
                )
            ),
            data_version=get_setting(
                properties,
                'GEO_EDGEKV_DATA_VERSION',
                'edgekv.data_version',
                '2.0',
            ),
        )


def build_key_source(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f'Invalid source URL for EdgeKV key: {url}')

    path = parsed.path or '/'
    if parsed.query:
        path = f'{path}?{parsed.query}'
    return f'{parsed.netloc}{path}'


def generate_edgekv_key(url: str) -> str:
    key_source = build_key_source(url)
    encoded = base64.urlsafe_b64encode(key_source.encode('utf-8')).decode('ascii')
    return f"k{encoded.replace('=', '')}"


def utc_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def load_edgerc(path: Path, section: str) -> dict[str, str]:
    parser = configparser.ConfigParser()
    read_files = parser.read(path)
    if not read_files:
        raise FileNotFoundError(f'Unable to read EdgeGrid credentials file: {path}')
    if not parser.has_section(section):
        raise ValueError(f'Missing section [{section}] in EdgeGrid credentials file: {path}')

    credentials = {key: value for key, value in parser.items(section)}
    required = {'host', 'client_token', 'client_secret', 'access_token'}
    missing = sorted(required - set(credentials))
    if missing:
        raise ValueError(
            f"Missing required EdgeGrid credential(s) in [{section}]: {', '.join(missing)}"
        )
    return credentials


def build_edgekv_payload(
    *,
    source_url: str,
    enriched_html: str,
    geo_result_json: dict[str, Any],
    injection_report: dict[str, Any],
    settings: EdgeKVSettings,
) -> dict[str, Any]:
    updated_at = datetime.now(timezone.utc)
    page_type_match = resolve_page_type(source_url)
    ttl_seconds = page_type_match.ttl_seconds
    expires_at = updated_at + timedelta(seconds=ttl_seconds)
    return {
        'version': settings.data_version,
        'updated_at': utc_timestamp(updated_at),
        'ttl_seconds': ttl_seconds,
        'expires_at': utc_timestamp(expires_at),
        'html': enriched_html,
    }


class EdgeKVClient:
    def __init__(self, settings: EdgeKVSettings):
        self.settings = settings
        self.credentials = load_edgerc(settings.edgerc_path, settings.edgerc_section)

    def _session(self):
        try:
            import requests
            from akamai.edgegrid import EdgeGridAuth
        except ImportError as exc:
            raise RuntimeError(
                'EdgeKV publishing requires requests and edgegrid-python. '
                'Install dependencies with: pip install -r requirements.txt'
            ) from exc

        session = requests.Session()
        session.auth = EdgeGridAuth(
            client_token=self.credentials['client_token'],
            client_secret=self.credentials['client_secret'],
            access_token=self.credentials['access_token'],
        )
        return session

    def item_url(self, item_id: str) -> str:
        path = DEFAULT_EDGEKV_API_PATH_TEMPLATE.format(
            network=quote(self.settings.network, safe=''),
            namespace=quote(self.settings.namespace, safe=''),
            group_id=quote(self.settings.group_id, safe=''),
            item_id=quote(item_id, safe=''),
        )
        return f"https://{self.credentials['host']}{path}"

    def put_item(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._session()
        response = session.put(
            self.item_url(item_id),
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            timeout=self.settings.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f'EdgeKV PUT failed: status={response.status_code}, body={response.text[:2000]}'
            )

        return {
            'status_code': response.status_code,
            'item_id': item_id,
            'url': self.item_url(item_id),
            'response_text': response.text,
        }


def publish_edgekv_item(
    *,
    source_url: str,
    enriched_html: str,
    geo_result_json: dict[str, Any],
    injection_report: dict[str, Any],
    settings: EdgeKVSettings | None = None,
) -> dict[str, Any] | None:
    effective_settings = settings or EdgeKVSettings.from_env_and_properties()
    if not effective_settings.enabled:
        return None

    item_id = generate_edgekv_key(source_url)
    payload = build_edgekv_payload(
        source_url=source_url,
        enriched_html=enriched_html,
        geo_result_json=geo_result_json,
        injection_report=injection_report,
        settings=effective_settings,
    )
    return EdgeKVClient(effective_settings).put_item(item_id, payload)
