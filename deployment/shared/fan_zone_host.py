"""Match task-selected identities and destinations to the installed host registry."""
import json
from pathlib import Path
import stat
from fan_zone_config import validate_site, validate_sites

SETTINGS = Path('/opt/fanzone-shared/settings.json')
IDENTITY = ('slug', 'name', 'city', 'abbreviation', 'website_root',
            'nfl_snapshot_dir', 'recap_snapshot_dir')


def authorize_site(site, pipeline, settings_path=SETTINGS):
    if pipeline not in ('nfl', 'recaps'):
        raise ValueError('Unknown host pipeline')
    selected = validate_site(site.get('slug'), site)
    path = Path(settings_path)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Host registry may not contain symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Host registry must be an ordinary root-owned file without group/other write access')
    registered = validate_sites(json.loads(path.read_text())['sites']).get(selected['slug'])
    if registered is None:
        raise ValueError('Install this team in the host registry before running its tasks')
    for key in IDENTITY:
        if key not in registered or selected.get(key) != registered[key]:
            raise ValueError(f"Task {key} differs from the installed host registry")
    known_id = registered.get('balldontlie_team_id')
    requested_id = selected.get('balldontlie_team_id')
    if known_id is not None and requested_id not in (None, known_id):
        raise ValueError('Task team identifier differs from the installed host registry')
    if requested_id is None and known_id is not None:
        selected['balldontlie_team_id'] = known_id
    # Prompts and enabled flags are intentionally read from the Airflow Variable.
    # A path/identity change requires the corresponding host setup to be installed.
    return selected
