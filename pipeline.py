import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if (ROOT.parent / '.python_packages').exists():
    sys.path.insert(0, str(ROOT.parent / '.python_packages'))

import yaml
from bs4 import BeautifulSoup

from matching import ACCOUNT_FIELDS, CONTACT_FIELDS, build_proposals, chow_needed, csv_rows, equivalent, location, proposed_rows, short_id, text

DATA = ROOT / 'data'


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.stem + '.', suffix='.tmp', delete=False) as handle:
        json.dump(value, handle, indent=2)
        temporary = Path(handle.name)
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)


def save_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(key for row in rows for key in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def config():
    return yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))


def state():
    return read_json(DATA / 'review.json', {'decisions': {}, 'matches': {}, 'created_ids': {}, 'unfinished_creates': {}})


def approval_options(proposal):
    options = []
    for index, item in enumerate(proposal['operations']):
        fields = [None] if item['action'] == 'create' else list(item['changes'])
        for field in fields:
            signature = {'entity': item['entity'], 'action': item['action'], 'record_id': item['record_id'],
                         'field': field, 'changes': item['changes'] if field is None else item['changes'][field]}
            options.append({'key': short_id(json.dumps(signature, sort_keys=True)), 'operation': index, 'field': field})
    return options


def proposal_status(proposal, saved):
    decision = saved['decisions'].get(proposal['id'])
    if decision:
        return decision['status']
    options = approval_options(proposal)
    if options and all(saved.get('field_decisions', {}).get(option['key']) in ('rejected', 'applied') for option in options):
        return 'decided'
    return 'pending'


def selected_keys(proposal, saved):
    decision = saved['decisions'].get(proposal['id'], {})
    if 'selected' in decision:
        return decision['selected']
    return [option['key'] for option in approval_options(proposal)
            if saved.get('field_decisions', {}).get(option['key']) not in ('rejected', 'applied')]


def selected_operations(proposal, selection):
    keys = set(selection)
    output = []
    options = approval_options(proposal)
    if keys - {option['key'] for option in options}:
        raise ValueError('The selected fields changed. Refresh and review again.')
    for index, item in enumerate(proposal['operations']):
        fields = [option['field'] for option in options if option['operation'] == index and option['key'] in keys]
        if not fields:
            continue
        output.append(dict(item) if item['action'] == 'create' else
                      dict(item, payload={field: item['payload'][field] for field in fields},
                           changes={field: item['changes'][field] for field in fields}))
    return output


def validate_selection(proposal, selection):
    chosen = selected_operations(proposal, selection)
    if not chosen:
        raise ValueError('Select at least one change to approve, or reject the proposal.')
    creates = {item['record_id'] for item in chosen if item['action'] == 'create'}
    for item in chosen:
        for field in ('account_id', 'chow_current_account', 'duplicate_of_account'):
            value = item['payload'].get(field, '')
            if isinstance(value, str) and value.startswith('NEW_') and value not in creates:
                raise ValueError('Also select creation of the related account, or deselect its contact/link.')
    if proposal['category'] == 'Change of ownership':
        required = [item for item in proposal['operations'] if item['entity'] == 'account']
        ownership_chosen = [item for item in chosen if item['entity'] == 'account']
        if ownership_chosen and ownership_chosen != required:
            raise ValueError('The new CHOW account and old-account link must be approved together.')
    for item in chosen:
        original = next(original for original in proposal['operations']
                        if (original['entity'], original['record_id']) == (item['entity'], item['record_id']))
        if 'duplicate_of_account' in original['payload'] and item['payload'] != original['payload']:
            raise ValueError('A duplicate’s inactive status, survivor link and note must be approved together.')
    replacements = [item for item in proposal['operations'] if item['entity'] == 'contact' and
                    (item['action'] == 'create' or item['payload'].get('is_active') is False)]
    if any(item['payload'].get('is_active') is False for item in replacements):
        replacement_ids = {item['record_id'] for item in replacements}
        selected_replacements = [item for item in chosen if item['record_id'] in replacement_ids]
        if selected_replacements and selected_replacements != replacements:
            raise ValueError('Creating a replacement administrator and deactivating the old role must be approved together.')
    return chosen


@contextmanager
def busy():
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / '.busy'
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError('A refresh or apply is already running. Please wait.') from None
    os.close(descriptor)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def token():
    for path in (ROOT / '.env', ROOT.parent / '.env'):
        if path.exists():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=', 1)
                    if not os.environ.get(key.strip()):
                        os.environ[key.strip()] = value.strip().strip('\"').strip("'")
    value = next((os.getenv(key) for key in ('BH_API_TOKEN', 'API_TOKEN', 'TOKEN') if os.getenv(key)), None)
    if not value:
        raise ValueError('Add the CRM API token to the project .env file before refreshing or applying.')
    return value


def api(method, path, payload=None):
    request = urllib.request.Request(config()['api'].rstrip('/') + path,
                                     data=None if payload is None else json.dumps(payload).encode(), method=method,
                                     headers={'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def all_records(entity):
    rows, seen = [], set()
    for page in range(1, 101):
        response = api('GET', f'/{entity}s?page={page}&page_size=100')
        batch = response if isinstance(response, list) else next((response[key] for key in ('data', 'items', entity + 's', 'results') if isinstance(response.get(key), list)), None)
        if batch is None:
            raise ValueError('Unexpected CRM response. Export stopped instead of saving incomplete data.')
        total = response.get('total', response.get('total_count')) if isinstance(response, dict) else None
        if not batch:
            if total is not None and len(rows) != int(total):
                raise ValueError('CRM export ended before all records were received.')
            return rows
        for row in batch:
            if row[entity + '_id'] in seen:
                raise ValueError('CRM pagination repeated records. Export stopped.')
            seen.add(row[entity + '_id'])
            rows.append(row)
        if total is not None and len(rows) == int(total):
            return rows
    raise ValueError('CRM pagination limit reached. Export stopped.')


def soup(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Bellhaven-demo/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return BeautifulSoup(response.read(), 'html.parser')


def website_records():
    settings = config()
    base = settings['website'].rstrip('/')
    pending, visited, detail_urls = [base + '/communities'], set(), set()
    while pending:
        url = pending.pop()
        if url in visited:
            continue
        if len(visited) >= 100:
            raise ValueError('Website pagination limit reached.')
        visited.add(url)
        page = soup(url)
        for anchor in page.select('.cardgrid .card h3 a[href]'):
            detail_urls.add(urllib.parse.urljoin(base, anchor['href']))
        for anchor in page.select('a[href]'):
            link = urllib.parse.urljoin(base, anchor['href'])
            parsed = urllib.parse.urlparse(link)
            if parsed.netloc == urllib.parse.urlparse(base).netloc and parsed.path == '/communities' and 'page=' in parsed.query:
                pending.append(link)
    detail_urls.update(urllib.parse.urljoin(base, path) for path in settings.get('extra_community_pages', []))
    if not detail_urls:
        raise ValueError('No website locations found. Existing data was kept.')
    rows = []
    for url in sorted(detail_urls):
        page = soup(url)
        heading = page.select_one('h1')
        row = {'name': heading.get_text(' ', strip=True) if heading else '', 'street_address': '', 'city': '', 'state': '', 'zip': '', 'care_offerings': '', 'administrator': '', 'phone': '', 'url': url}
        for label in page.select('dl.detail dt'):
            value = label.find_next_sibling('dd')
            if value is None:
                continue
            key = label.get_text(strip=True).lower()
            if key == 'address':
                lines = list(value.stripped_strings)
                match = re.fullmatch(r'(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?', lines[-1] if lines else '')
                if not match:
                    raise ValueError('Could not read address: ' + url)
                row.update(street_address=', '.join(lines[:-1]), city=match[1], state=match[2], zip=match[3])
            elif key.startswith('care'):
                badges = [item.get_text(strip=True) for item in value.select('.badge')]
                row['care_offerings'] = '; '.join(badges) or value.get_text(' ', strip=True)
            elif key in ('administrator', 'phone'):
                row[key] = value.get_text(' ', strip=True)
        if any(not row[key] for key in ('name', 'street_address', 'city', 'state', 'zip', 'care_offerings')):
            raise ValueError('Incomplete website location: ' + url)
        rows.append(row)
    return rows


def differences(previous, current):
    changes = []
    if previous is None:
        return changes
    for source, identity in [('website', 'url'), ('accounts', 'account_id'), ('contacts', 'contact_id')]:
        before = {row[identity]: row for row in previous[source]}
        after = {row[identity]: row for row in current[source]}
        for record_id in sorted(before.keys() | after.keys()):
            old, new = before.get(record_id), after.get(record_id)
            name = (new or old).get('name', record_id)
            if old is None or new is None:
                changes.append({'source': source, 'record_id': record_id, 'name': name, 'change': 'Added' if old is None else 'Removed', 'field': 'record', 'previous': old, 'current': new})
                continue
            for field in sorted(old.keys() | new.keys()):
                if field in (identity, 'updated_at', 'created_by_candidate'):
                    continue
                if str(old.get(field, '')) != str(new.get(field, '')):
                    changes.append({'source': source, 'record_id': record_id, 'name': name, 'change': 'Changed', 'field': field, 'previous': old.get(field, ''), 'current': new.get(field, '')})
    for change in changes:
        change['id'] = 'change_' + short_id(json.dumps(change, sort_keys=True))
    return changes


def make_report(snapshot, previous=None, previous_date=None):
    saved = state()
    proposals, matches = build_proposals(snapshot, config(), saved['matches'], csv_rows(ROOT / 'matching_choices.csv'), csv_rows(ROOT / 'duplicates.csv'))
    changes = differences(previous, snapshot)
    report = {'fetched_at': snapshot['fetched_at'], 'previous_date': previous_date,
              'counts': {source: len(snapshot[source]) for source in ('website', 'accounts', 'contacts')},
              'source_changes': changes, 'matches': matches, 'proposals': proposals}
    save_json(DATA / 'latest.json', snapshot)
    save_json(DATA / 'report.json', report)
    save_json(DATA / 'review.json', saved)
    preview = [dict(item, operations=selected_operations(item, selected_keys(item, saved)))
               for item in proposals if proposal_status(item, saved) not in ('rejected', 'decided')]
    proposed_accounts, proposed_contacts = proposed_rows(snapshot, preview, config())
    save_csv(DATA / 'crm_clean.csv', proposed_accounts)
    save_csv(DATA / 'contacts_clean.csv', proposed_contacts)
    save_csv(DATA / 'matches.csv', matches, ['name', 'account_id', 'classification'])
    printable = [{key: json.dumps(value) if isinstance(value, (dict, list)) else value for key, value in row.items()} for row in changes]
    save_csv(DATA / 'daily_changes.csv', printable, ['id', 'source', 'record_id', 'name', 'change', 'field', 'previous', 'current'])
    return report


def refresh(offline=None):
    with busy():
        snapshot = {'fetched_at': datetime.now(timezone.utc).isoformat()}
        if offline:
            folder = Path(offline)
            snapshot.update(accounts=csv_rows(folder / 'crm_raw.csv'), contacts=csv_rows(folder / 'contacts_raw.csv'), website=csv_rows(folder / 'communities.csv'))
        else:
            snapshot.update(accounts=all_records('account'), contacts=all_records('contact'), website=website_records())
        if not snapshot['accounts'] or not snapshot['website']:
            raise ValueError('Empty source data. Existing reports were kept.')
        for source, identity in [('accounts', 'account_id'), ('contacts', 'contact_id'), ('website', 'url')]:
            if len({row[identity] for row in snapshot[source]}) != len(snapshot[source]):
                raise ValueError('Duplicate source record IDs. Existing reports were kept.')
        today = datetime.now().astimezone().date().isoformat()
        folder = DATA / 'snapshots'
        prior_files = sorted(path for path in folder.glob('*.json') if path.stem < today) if folder.exists() else []
        previous_file = prior_files[-1] if prior_files else None
        report = make_report(snapshot, read_json(previous_file) if previous_file else None, previous_file.stem if previous_file else None)
        save_json(folder / (today + '.json'), snapshot)
        for source, filename in [('accounts', 'crm_raw.csv'), ('contacts', 'contacts_raw.csv'), ('website', 'communities.csv')]:
            save_csv(DATA / filename, snapshot[source])
        return report


def decide(proposal_id, decision, name, note='', selection=None):
    if decision not in ('approved', 'rejected', 'acknowledged'):
        raise ValueError('Invalid decision')
    with busy():
        saved = state()
        report = read_json(DATA / 'report.json', {})
        valid = {item['id'] for item in report.get('proposals', []) + report.get('source_changes', [])}
        if proposal_id not in valid:
            raise ValueError('This item changed. Refresh the page and review the latest version.')
        if saved['decisions'].get(proposal_id, {}).get('status') == 'applied':
            raise ValueError('This change has already been applied.')
        record = {'status': decision, 'name': name, 'note': note, 'decided_at': datetime.now(timezone.utc).isoformat()}
        proposal = next((item for item in report.get('proposals', []) if item['id'] == proposal_id), None)
        if proposal and proposal['operations']:
            options = approval_options(proposal)
            keys = [option['key'] for option in options]
            chosen = list(selection) if selection is not None else selected_keys(proposal, saved)
            if decision == 'approved':
                validate_selection(proposal, chosen)
            else:
                chosen = []
            record.update(selected=chosen, selected_count=len(chosen), total_count=len(keys))
            fields = saved.setdefault('field_decisions', {})
            for key in keys:
                fields[key] = 'approved' if key in chosen else 'rejected'
        saved['decisions'][proposal_id] = record
        save_json(DATA / 'review.json', saved)


def confirm_match(url, account_id):
    with busy():
        saved = state()
        saved['matches'][url] = account_id
        save_json(DATA / 'review.json', saved)
        snapshot = read_json(DATA / 'latest.json')
        report = read_json(DATA / 'report.json')
        previous = read_json(DATA / 'snapshots' / (report['previous_date'] + '.json')) if report.get('previous_date') else None
        return make_report(snapshot, previous, report.get('previous_date'))


def response_record(response, entity):
    if isinstance(response, dict) and response.get(entity + '_id'):
        return response
    for key in ('data', entity):
        if isinstance(response, dict) and isinstance(response.get(key), dict):
            return response_record(response[key], entity)
    raise ValueError('The API did not return a record ID. Refresh and check the CRM before trying again.')


def resolved(payload, saved):
    output = dict(payload)
    for field in ('account_id', 'parent_id', 'chow_current_account', 'duplicate_of_account'):
        value = output.get(field, '')
        if isinstance(value, str) and value.startswith('NEW_'):
            if value not in saved['created_ids']:
                raise ValueError('The related account has not been created yet.')
            output[field] = saved['created_ids'][value]
    return output


def check_current(item, current, payload):
    fields = ACCOUNT_FIELDS + ['lifetime_revenue', 'outstanding_ar'] if item['entity'] == 'account' else CONTACT_FIELDS
    for field in fields:
        if not equivalent(current.get(field, ''), item['before'].get(field, '')) and not (field in payload and equivalent(current.get(field, ''), payload[field])):
            raise ValueError('CRM changed after this proposal was reviewed. Refresh and review the new correction.')
    if item['entity'] == 'account':
        if 'parent_id' in payload and payload['parent_id'] != current.get('parent_id') and chow_needed(current):
            raise ValueError('Current billing data requires CHOW. Refresh to get the correct proposal.')
        if payload.get('chow_current_account') and chow_needed(current) and set(payload) != {'chow_current_account'}:
            raise ValueError('A protected CHOW account may only receive its new-account link.')


def verify_payload(item, record, payload):
    mismatches = {field: {'approved': value, 'CRM': record.get(field, '<not returned>')}
                  for field, value in payload.items() if not equivalent(record.get(field), value)}
    if mismatches:
        record_id = record.get(item['entity'] + '_id', item['record_id'])
        raise ValueError('API read-back mismatch for ' + item['entity'] + ' ' + record_id + ': ' +
                         json.dumps(mismatches) + '. Refresh and inspect before continuing.')


def apply_approved():
    results = []
    with busy():
        saved = state()
        report = read_json(DATA / 'report.json', {})
        approved = [item for item in report.get('proposals', []) if item['operations'] and saved['decisions'].get(item['id'], {}).get('status') == 'approved']
        if not approved:
            return results
        existing = {'account': all_records('account'), 'contact': all_records('contact')}
        for proposal in approved:
            try:
                operations = validate_selection(proposal, selected_keys(proposal, saved))
                if proposal['category'] == 'Change of ownership':
                    old = proposal['evidence'].get('chow_old', proposal['evidence']['crm'])
                    current = response_record(api('GET', '/accounts/' + old['account_id']), 'account')
                    if not chow_needed(current):
                        raise ValueError('Billing changed. Refresh this ownership proposal before applying.')
                    pointer = next(item for item in proposal['operations'] if item['entity'] == 'account' and item['record_id'] == old['account_id'] and 'chow_current_account' in item['payload'])
                    linked = pointer['payload']['chow_current_account']
                    allowed = {'chow_current_account': saved['created_ids'].get(linked, linked)} if not linked.startswith('NEW_') or linked in saved['created_ids'] else {}
                    check_current({'entity': 'account', 'before': old}, current, allowed)
                for step, item in enumerate(operations):
                    payload = resolved(item['payload'], saved)
                    collection = '/' + item['entity'] + 's'
                    if item['action'] == 'create':
                        local_id = item['record_id']
                        if local_id in saved['created_ids']:
                            existing_record = response_record(api('GET', collection + '/' + saved['created_ids'][local_id]), item['entity'])
                            verify_payload(item, existing_record, payload)
                            continue
                        if local_id in saved['unfinished_creates']:
                            raise ValueError('A previous create has an unknown result. Refresh and check the CRM before retrying; another record will not be created automatically.')
                        old_id = (proposal['evidence'].get('crm') or {}).get('account_id')
                        for row in existing[item['entity']]:
                            if item['entity'] == 'account' and row['account_id'] == old_id:
                                continue
                            same_place = location(row) == location(payload) if item['entity'] == 'account' else row.get('account_id') == payload.get('account_id')
                            if text(row.get('name')) == text(payload.get('name')) and same_place:
                                raise ValueError('A matching record already exists. Refresh instead of creating a duplicate.')
                        saved['unfinished_creates'][local_id] = {'name': proposal['name'], 'payload': payload}
                        save_json(DATA / 'review.json', saved)
                        created = response_record(api('POST', collection, payload), item['entity'])
                        saved['created_ids'][local_id] = created[item['entity'] + '_id']
                        saved['unfinished_creates'].pop(local_id, None)
                        save_json(DATA / 'review.json', saved)
                        existing[item['entity']].append(created)
                        verified = response_record(api('GET', collection + '/' + saved['created_ids'][local_id]), item['entity'])
                    else:
                        path = collection + '/' + item['record_id']
                        current = response_record(api('GET', path), item['entity'])
                        check_current(item, current, payload)
                        if any(not equivalent(current.get(field), value) for field, value in payload.items()):
                            api('PATCH', path, payload)
                        verified = response_record(api('GET', path), item['entity'])
                        if item['entity'] == 'account' and 'chow_current_account' in payload:
                            for field in ACCOUNT_FIELDS + ['lifetime_revenue', 'outstanding_ar']:
                                if field != 'chow_current_account' and not equivalent(current.get(field), verified.get(field)):
                                    raise ValueError('The old CHOW account changed unexpectedly. Inspect before continuing.')
                    verify_payload(item, verified, payload)
                saved['decisions'][proposal['id']]['status'] = 'applied'
                saved['decisions'][proposal['id']]['applied_at'] = datetime.now(timezone.utc).isoformat()
                for key in selected_keys(proposal, saved):
                    saved.setdefault('field_decisions', {})[key] = 'applied'
                save_json(DATA / 'review.json', saved)
                results.append({'name': proposal['name'], 'status': 'applied'})
            except Exception as error:
                results.append({'name': proposal['name'], 'status': 'stopped', 'reason': str(error)})
                break
        save_json(DATA / 'apply_results.json', results)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Download all sources and prepare corrections for review. No CRM writes.')
    parser.add_argument('--offline', type=Path, help='Use existing CSV exports from this folder instead of fetching sources.')
    args = parser.parse_args()
    report = refresh(args.offline)
    saved = state()
    pending = sum(proposal_status(item, saved) == 'pending' for item in report['proposals'])
    print(json.dumps({'sources': report['counts'], 'compared_with': report['previous_date'] or 'First run: baseline saved', 'daily_differences': len(report['source_changes']), 'pending_corrections': pending}, indent=2))
