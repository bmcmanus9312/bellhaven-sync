import csv
import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path


ACCOUNT_FIELDS = ['name', 'parent_id', 'billing_street', 'billing_city', 'billing_state',
                  'billing_zip', 'care_type', 'status', 'phone', 'note',
                  'chow_current_account', 'duplicate_of_account']
CONTACT_FIELDS = ['account_id', 'name', 'title', 'email', 'phone', 'is_active']


def text(value):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower().replace('&', ' and '))).strip()


def address(value):
    words = {'street': 'st', 'avenue': 'ave', 'road': 'rd', 'boulevard': 'blvd', 'lane': 'ln',
             'drive': 'dr', 'court': 'ct', 'pike': 'pk', 'north': 'n', 'south': 's', 'east': 'e', 'west': 'w'}
    return ' '.join(words.get(word, word) for word in text(value).split())


def zip5(value):
    found = re.search(r'(?<!\d)\d{5}(?!\d)', str(value or ''))
    return found[0] if found else ''


def phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return digits[1:] if len(digits) == 11 and digits.startswith('1') else digits


def location(row, website=False):
    return (address(row.get('street_address' if website else 'billing_street')),
            text(row.get('city' if website else 'billing_city')),
            text(row.get('state' if website else 'billing_state')),
            zip5(row.get('zip' if website else 'billing_zip')))


def chow_needed(row):
    amounts = []
    for field in ('lifetime_revenue', 'outstanding_ar'):
        try:
            amount = Decimal(str(row.get(field, '')).replace(',', '').replace('$', ''))
        except InvalidOperation:
            raise ValueError('Billing amounts are missing or invalid. Review before changing the parent.') from None
        if not amount.is_finite() or amount < 0:
            raise ValueError('Billing amounts are invalid. Review before changing the parent.')
        amounts.append(amount)
    return all(amount > 0 for amount in amounts)


def equivalent(left, right):
    return str('' if left is None else left).lower() == str('' if right is None else right).lower()


def short_id(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def choose_account(community, accounts, contacts, hints, choices, parent_id):
    identity = community['url']
    excluded = {hint['account_id'] for hint in hints if hint['website_name'] == community['name'] and hint['relationship'] == 'different'}
    available = [row for row in accounts if row['account_id'] not in excluded and row.get('billing_street') and not row.get('duplicate_of_account') and not row.get('chow_current_account')]
    candidates = []
    for row in available:
        same_address = all(location(community, True)) and location(row) == location(community, True)
        same_name = text(row['name']) == text(community['name'])
        same_phone = bool(phone(community['phone'])) and phone(row.get('phone')) == phone(community['phone'])
        same_admin = any(contact['account_id'] == row['account_id'] and text(contact['name']) == text(community['administrator']) for contact in contacts)
        if same_address or same_name or same_phone or same_admin:
            candidates.append({'account': row, 'same_address': same_address, 'same_name': same_name,
                               'same_phone': same_phone, 'same_administrator': same_admin})
    current = [item['account'] for item in candidates if item['same_name'] and item['same_address'] and item['account'].get('parent_id') == parent_id]
    if len(current) == 1:
        return current[0], candidates, 'Matched using current name and address'
    choice = choices.get(identity)
    if choice == 'new':
        return None, [], 'Reviewer confirmed a separate new facility'
    known_id = choice or next((hint['account_id'] for hint in hints if hint['website_name'] == community['name'] and hint['relationship'] == 'same'), '')
    by_id = {row['account_id']: row for row in accounts}
    if known_id in by_id and known_id not in excluded:
        row, visited = by_id[known_id], set()
        while row.get('chow_current_account'):
            target = row['chow_current_account']
            if target not in by_id or target in visited:
                return None, candidates, 'CHOW link needs review'
            visited.add(target)
            row = by_id[target]
        if not row.get('duplicate_of_account'):
            return row, candidates, 'Previously investigated facility identity'
    strong = [item['account'] for item in candidates if item['same_address'] and (item['same_name'] or item['same_phone'] or item['same_administrator'])]
    if len(strong) == 1:
        return strong[0], candidates, 'Matched address with name, phone or administrator evidence'
    return None, candidates, 'Unsure which match' if candidates else 'No matching CRM facility found'


def operation(entity, record_id, before, desired):
    changes = {field: {'before': before.get(field, '') if before else '', 'after': value}
               for field, value in desired.items() if before is None or not equivalent(before.get(field, ''), value)}
    if not changes:
        return None
    return {'entity': entity, 'action': 'update' if before else 'create', 'record_id': record_id,
            'before': before, 'payload': {field: item['after'] for field, item in changes.items()}, 'changes': changes}


def make_proposal(name, category, reason, operations, evidence):
    operations = [item for item in operations if item]
    signature = [{'entity': item['entity'], 'action': item['action'], 'record_id': item['record_id'],
                  'changes': item['changes'], 'billing': {field: (item.get('before') or {}).get(field) for field in ('parent_id', 'lifetime_revenue', 'outstanding_ar')}} for item in operations]
    identity = json.dumps({'name': name, 'category': category, 'operations': signature,
                           'evidence': evidence if not operations else None}, sort_keys=True)
    return {'id': short_id(identity), 'name': name, 'category': category, 'reason': reason,
            'operations': operations, 'evidence': evidence}


def build_proposals(snapshot, config, choices, hints, duplicates):
    accounts, contacts, website = snapshot['accounts'], snapshot['contacts'], snapshot['website']
    by_id = {row['account_id']: row for row in accounts}
    if config['parent_id'] not in by_id:
        raise ValueError('Bellhaven parent is missing from the full CRM export.')
    proposals, matches, matched_ids, uncertain_ids = [], [], set(), set()
    care_types = {'assisted living': 'Assisted Living', 'memory support': 'Memory Care', 'memory care': 'Memory Care',
                  'short term rehabilitation and nursing': 'Skilled Nursing', 'skilled nursing': 'Skilled Nursing', 'independent living': 'Independent Living'}
    for community in website:
        account, candidates, reason = choose_account(community, accounts, contacts, hints, choices, config['parent_id'])
        evidence = {'website': community, 'crm': account, 'candidates': candidates}
        if account is None and candidates:
            uncertain_ids.update(item['account']['account_id'] for item in candidates)
            proposals.append(make_proposal(community['name'], 'Needs review', reason, [], evidence))
            matches.append({'name': community['name'], 'account_id': '', 'classification': 'Needs review'})
            continue
        care = care_types.get(text(community['care_offerings'].split(';', 1)[0]))
        if care is None:
            proposals.append(make_proposal(community['name'], 'Needs review', 'First website care offering has no CRM mapping.', [], evidence))
            matches.append({'name': community['name'], 'account_id': account['account_id'] if account else '', 'classification': 'Needs review'})
            continue
        desired = {'name': community['name'], 'parent_id': config['parent_id'], 'billing_street': community['street_address'],
                   'billing_city': community['city'], 'billing_state': community['state'], 'billing_zip': zip5(community['zip']),
                   'care_type': care, 'phone': community['phone'], 'status': 'Active'}
        if not community.get('phone') or len(phone(community['phone'])) != 10:
            desired.pop('phone')
            proposals.append(make_proposal(community['name'] + ' phone', 'Needs review', 'Website phone is missing or invalid; preserve the CRM phone until checked.', [], evidence))
        operations, category = [], 'Match needs a fix'
        if account:
            if account['account_id'] in matched_ids:
                raise ValueError('One CRM account matched multiple website communities. Review the saved matching choices.')
            matched_ids.add(account['account_id'])
            prior_id = next((hint['account_id'] for hint in hints if hint['website_name'] == community['name'] and hint['relationship'] == 'same'), '')
            prior = by_id.get(prior_id)
            if prior and prior_id != account['account_id'] and prior.get('parent_id') != config['parent_id'] and not prior.get('chow_current_account'):
                try:
                    if chow_needed(prior):
                        operations.append(operation('account', prior_id, prior, {'chow_current_account': account['account_id']}))
                        evidence['chow_old'] = prior
                        category, reason = 'Change of ownership', 'Current account already exists; link the preserved old billing account without creating another.'
                except ValueError:
                    proposals.append(make_proposal(prior['name'] + ' ownership link', 'Needs review', 'Check the old account billing values before linking it.', [], {'crm': prior}))
            try:
                chow = account.get('parent_id') != config['parent_id'] and chow_needed(account)
            except ValueError as error:
                proposals.append(make_proposal(community['name'], 'Needs review', str(error), [], evidence))
                matches.append({'name': community['name'], 'account_id': account['account_id'], 'classification': 'Needs review'})
                continue
            if chow:
                evidence['chow_old'] = account
                account_id = 'NEW_ACCOUNT_' + short_id(community['url'] + ':' + account['account_id'])
                desired['note'] = 'Website source: ' + community['url']
                operations += [operation('account', account_id, None, desired),
                               operation('account', account['account_id'], account, {'chow_current_account': account_id})]
                category, reason = 'Change of ownership', 'Revenue history and outstanding AR: preserve old account; create new under Bellhaven and link the old account.'
            else:
                account_id = account['account_id']
                operations.append(operation('account', account_id, account, desired))
        else:
            account_id = 'NEW_ACCOUNT_' + short_id(community['url'])
            desired['note'] = 'Website source: ' + community['url']
            operations.append(operation('account', account_id, None, desired))
            category = 'Missing CRM facility'
        account_contacts = [row for row in contacts if row['account_id'] == account_id]
        people = [row for row in account_contacts if text(row['name']) == text(community['administrator'])]
        if not community.get('administrator'):
            proposals.append(make_proposal(community['name'] + ' administrator', 'Needs review', 'Website administrator is missing; preserve existing contacts until checked.', [], evidence))
        elif len(people) > 1:
            proposals.append(make_proposal(community['name'] + ' contact', 'Needs review', 'Multiple contacts match the administrator; choose a contact survivor before editing.', [], evidence))
        else:
            contact_id = people[0]['contact_id'] if people else 'NEW_CONTACT_' + short_id(community['url'] + ':' + text(community['administrator']))
            desired_contact = {'account_id': account_id, 'name': community['administrator'], 'title': 'Administrator', 'phone': community['phone'], 'is_active': True}
            if 'phone' not in desired:
                desired_contact.pop('phone')
            if not people:
                desired_contact['email'] = ''
            operations.append(operation('contact', contact_id, people[0] if people else None, desired_contact))
            for previous in account_contacts:
                if previous['contact_id'] != contact_id and text(previous.get('title')) == 'administrator' and str(previous.get('is_active')).lower() == 'true':
                    operations.append(operation('contact', previous['contact_id'], previous, {'is_active': False}))
        operations = [item for item in operations if item]
        classification = category if operations else 'Confident match'
        matches.append({'name': community['name'], 'account_id': account_id, 'classification': classification})
        if operations:
            proposals.append(make_proposal(community['name'], category, reason + '. Website is authoritative; use the first care offering and published contact phone.', operations, evidence))
    for pair in duplicates:
        loser, survivor = by_id.get(pair['loser_id']), by_id.get(pair['survivor_id'])
        if not loser or not survivor or survivor['account_id'] not in matched_ids:
            continue
        if location(loser) != location(survivor) or loser['account_id'] in matched_ids or loser.get('chow_current_account'):
            proposals.append(make_proposal(loser['name'], 'Needs review', 'Previously selected duplicate relationship needs checking.', [], {'crm': loser, 'survivor': survivor}))
            continue
        note = 'Duplicate of ' + survivor['account_id'] + '; preserve history.'
        previous_note = loser.get('note') or ''
        desired = {'status': 'Inactive', 'duplicate_of_account': survivor['account_id'], 'note': previous_note if note in previous_note else previous_note + ('; ' if previous_note else '') + note}
        change = operation('account', loser['account_id'], loser, desired)
        if change:
            proposals.append(make_proposal(loser['name'], 'Duplicate', 'Previously investigated same-location duplicate; keep the record and identify the survivor.', [change], {'crm': loser, 'survivor': survivor}))
    for row in accounts:
        if row.get('parent_id') != config['parent_id'] or row['account_id'] in matched_ids | uncertain_ids or row.get('duplicate_of_account') or row.get('chow_current_account'):
            continue
        if any(pair['loser_id'] == row['account_id'] for pair in duplicates):
            continue
        note = 'not listed on Bellhaven website, closed/switched parent?'
        previous_note = row.get('note') or ''
        desired = {'status': 'Needs Review', 'note': previous_note if note in previous_note else previous_note + ('; ' if previous_note else '') + note}
        change = operation('account', row['account_id'], row, desired)
        if change:
            proposals.append(make_proposal(row['name'], 'Not on website', note, [change], {'crm': row}))
    return proposals, matches


def proposed_rows(snapshot, proposals, config):
    accounts, contacts = deepcopy(snapshot['accounts']), deepcopy(snapshot['contacts'])
    indexes = {'account': {row['account_id']: row for row in accounts}, 'contact': {row['contact_id']: row for row in contacts}}
    for proposal in proposals:
        for item in proposal['operations']:
            rows = accounts if item['entity'] == 'account' else contacts
            index = indexes[item['entity']]
            if item['record_id'] not in index:
                row = {field: '' for field in (accounts[0] if item['entity'] == 'account' else contacts[0] if contacts else CONTACT_FIELDS)}
                row[item['entity'] + '_id'] = item['record_id']
                if item['entity'] == 'account':
                    row.update(lifetime_revenue=0, outstanding_ar=0)
                rows.append(row)
                index[item['record_id']] = row
            index[item['record_id']].update(item['payload'])
            if item['entity'] == 'account' and item['payload'].get('parent_id') == config['parent_id']:
                index[item['record_id']]['parent_name'] = config['parent_name']
    return accounts, contacts
