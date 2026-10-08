import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline
from matching import build_proposals, csv_rows, proposed_rows


class DemoChecks(unittest.TestCase):
    def setUp(self):
        self.snapshot = pipeline.read_json(pipeline.ROOT / 'test_data' / 'source_snapshot.json')
        self.config = pipeline.config()
        self.hints = csv_rows(pipeline.ROOT / 'matching_choices.csv')
        self.duplicates = csv_rows(pipeline.ROOT / 'duplicates.csv')

    def proposals(self, snapshot=None):
        return build_proposals(snapshot or self.snapshot, self.config, {}, self.hints, self.duplicates)

    def test_json_save_retries_temporary_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'review.json'
            pipeline.save_json(target, {'decision': 'old'})
            original_replace = Path.replace
            attempts = []

            def locked_replace(temporary, destination):
                attempts.append(temporary)
                if len(attempts) < 3:
                    self.assertEqual(pipeline.read_json(target), {'decision': 'old'})
                    raise PermissionError('Simulated Windows sharing lock')
                return original_replace(temporary, destination)

            with patch.object(Path, 'replace', locked_replace), patch.object(pipeline.time, 'sleep') as sleep:
                pipeline.save_json(target, {'decision': 'approved'})
            self.assertEqual(pipeline.read_json(target), {'decision': 'approved'})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_json_save_permanent_lock_preserves_previous_decisions_and_unsaved_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'review.json'
            pipeline.save_json(target, {'decision': 'old'})
            with patch.object(Path, 'replace', side_effect=PermissionError('Simulated permanent lock')) as replace, patch.object(pipeline.time, 'sleep'):
                with self.assertRaises(PermissionError):
                    pipeline.save_json(target, {'decision': 'approved'})
            self.assertEqual(replace.call_count, 10)
            self.assertEqual(pipeline.read_json(target), {'decision': 'old'})
            copies = list(Path(folder).glob('review.*.tmp'))
            self.assertEqual(len(copies), 1)
            self.assertEqual(pipeline.read_json(copies[0]), {'decision': 'approved'})

    def test_all_communities_contacts_duplicates_and_chow(self):
        proposals, matches = self.proposals()
        accounts, contacts = proposed_rows(self.snapshot, proposals, self.config)
        self.assertEqual(len(matches), 35)
        self.assertTrue(all(item['account_id'] for item in matches))
        self.assertEqual(len(accounts), 126)
        self.assertEqual(len(contacts), 82)
        raw = {row['account_id']: row for row in self.snapshot['accounts']}
        cleaned = {row['account_id']: row for row in accounts}
        self.assertEqual(raw['001GNU41AVXZRLLJ9P'], cleaned['001GNU41AVXZRLLJ9P'])
        for old_id in ('001U6RW32TY0WSXZZB', '001A34WFSUYHCRBLFT'):
            changed = [field for field in raw[old_id] if raw[old_id][field] != cleaned[old_id][field]]
            self.assertEqual(changed, ['chow_current_account'])
        for duplicate in self.duplicates:
            row = cleaned[duplicate['loser_id']]
            self.assertEqual(row['status'], 'Inactive')
            self.assertEqual(row['duplicate_of_account'], duplicate['survivor_id'])

    def test_daily_comparison_covers_all_sources_and_ignores_timestamps(self):
        current = deepcopy(self.snapshot)
        current['website'][0]['phone'] = '2345678901'
        current['accounts'][0]['status'] = 'Needs Review'
        current['contacts'][0]['phone'] = '2345678902'
        for source in ('accounts', 'contacts'):
            for row in current[source]:
                row['updated_at'] = '2030-01-01T00:00:00Z'
        changes = pipeline.differences(self.snapshot, current)
        self.assertEqual(len(changes), 3)
        self.assertEqual({row['source'] for row in changes}, {'website', 'accounts', 'contacts'})
        self.assertEqual(pipeline.differences(None, current), [])

    def test_rejection_persists_but_changed_proposal_requires_review(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)):
            report = pipeline.make_report(self.snapshot)
            first = report['proposals'][0]
            pipeline.decide(first['id'], 'rejected', first['name'])
            repeated = pipeline.make_report(self.snapshot)
            self.assertIn(first['id'], {item['id'] for item in repeated['proposals']})
            self.assertEqual(pipeline.state()['decisions'][first['id']]['status'], 'rejected')
            current = deepcopy(self.snapshot)
            current['website'][0]['phone'] = '(234) 567-8901'
            changed = pipeline.make_report(current)
            new = next(item for item in changed['proposals'] if item['name'] == first['name'])
            self.assertNotEqual(new['id'], first['id'])
            self.assertNotIn(new['id'], pipeline.state()['decisions'])

    def test_refresh_uses_earlier_day_not_previous_refresh_same_day(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'all_records', side_effect=lambda entity: deepcopy(self.snapshot[entity + 's'])), patch.object(pipeline, 'website_records', return_value=self.snapshot['website']):
            pipeline.save_json(Path(folder) / 'snapshots' / '2000-01-01.json', self.snapshot)
            first = pipeline.refresh()
            second = pipeline.refresh()
            self.assertEqual(first['previous_date'], '2000-01-01')
            self.assertEqual(second['previous_date'], '2000-01-01')
            self.assertEqual(second['source_changes'], [])

    def test_unapproved_changes_cannot_call_api(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api') as api:
            pipeline.make_report(self.snapshot)
            self.assertEqual(pipeline.apply_approved(), [])
            api.assert_not_called()

    def test_simulated_apply_and_fresh_rerun_make_no_extra_records(self):
        accounts = {row['account_id']: deepcopy(row) for row in self.snapshot['accounts']}
        contacts = {row['contact_id']: deepcopy(row) for row in self.snapshot['contacts']}
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path))
            parts = path.strip('/').split('/')
            entity = 'account' if parts[0] == 'accounts' else 'contact'
            records = accounts if entity == 'account' else contacts
            if method == 'POST':
                record_id = 'REAL_' + entity + str(len(records))
                row = {field: '' for field in next(iter(records.values()))}
                row.update(payload)
                row.update({entity + '_id': record_id, 'created_by_candidate': True})
                if entity == 'account':
                    row.update(lifetime_revenue=0, outstanding_ar=0, parent_name=self.config['parent_name'])
                records[record_id] = row
                return deepcopy(row)
            if method == 'PATCH':
                records[parts[1]].update(payload)
                if entity == 'account' and 'parent_id' in payload:
                    records[parts[1]]['parent_name'] = self.config['parent_name']
            return deepcopy(records[parts[1]])

        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api', side_effect=fake_api), patch.object(pipeline, 'all_records', side_effect=lambda entity: deepcopy(list((accounts if entity == 'account' else contacts).values()))):
            report = pipeline.make_report(self.snapshot)
            for item in report['proposals']:
                pipeline.decide(item['id'], 'approved', item['name'])
            results = pipeline.apply_approved()
            self.assertFalse([row for row in results if row['status'] != 'applied'], results)
            self.assertEqual(sum(method == 'POST' and path == '/accounts' for method, path in calls), 5)
            self.assertEqual(sum(method == 'POST' and path == '/contacts' for method, path in calls), 15)
            self.assertEqual(pipeline.apply_approved(), [])
            current = deepcopy(self.snapshot)
            current.update(accounts=list(accounts.values()), contacts=list(contacts.values()))
            final = pipeline.make_report(current)
            self.assertEqual(final['proposals'], [])
            self.assertTrue(all(row['classification'] == 'Confident match' for row in final['matches']))

    def test_app_opens_and_reject_button_saves_decision(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)):
            report = pipeline.make_report(self.snapshot)
            app = AppTest.from_file(pipeline.ROOT / 'app.py', default_timeout=30).run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.tabs), 4)
            first = report['proposals'][0]
            app.button(key='reject_' + first['id']).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(pipeline.state()['decisions'][first['id']]['status'], 'rejected')

    def test_app_daily_comparison_can_be_reviewed_without_crm_writes(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api') as api:
            pipeline.save_json(Path(folder) / 'snapshots' / '2000-01-01.json', self.snapshot)
            current = deepcopy(self.snapshot)
            current['website'][0]['phone'] = '2345678901'
            report = pipeline.make_report(current, self.snapshot, '2000-01-01')
            app = AppTest.from_file(pipeline.ROOT / 'app.py', default_timeout=30).run()
            self.assertFalse(app.exception)
            next(button for button in app.button if button.label == 'Mark displayed differences reviewed').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(pipeline.state()['decisions'][report['source_changes'][0]['id']]['status'], 'acknowledged')
            api.assert_not_called()


    def test_partial_phone_approval_writes_only_selected_record_and_stays_decided(self):
        for selected_entity in ('account', 'contact'):
            with self.subTest(selected_entity=selected_entity), tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)):
                current = deepcopy(self.snapshot)
                records = {entity: {row[entity + '_id']: row for row in current[entity + 's']} for entity in ('account', 'contact')}
                writes = []

                def fake_api(method, path, payload=None):
                    collection, record_id = path.strip('/').split('/')
                    entity = collection[:-1]
                    if method == 'PATCH':
                        writes.append((entity, record_id, deepcopy(payload)))
                        records[entity][record_id].update(payload)
                    else:
                        self.assertEqual(method, 'GET')
                    return deepcopy(records[entity][record_id])

                report = pipeline.make_report(current)
                item = next(item for item in report['proposals'] if item['name'] == 'Bellhaven Shores of Erie')
                options = pipeline.approval_options(item)
                chosen = [option['key'] for option in options if item['operations'][option['operation']]['entity'] == selected_entity]
                self.assertEqual(len(chosen), 1)
                pipeline.decide(item['id'], 'approved', item['name'], selection=chosen)
                pipeline.make_report(current)
                skipped_entity = 'contact' if selected_entity == 'account' else 'account'
                skipped = next(operation for operation in item['operations'] if operation['entity'] == skipped_entity)
                preview_file = 'crm_clean.csv' if skipped_entity == 'account' else 'contacts_clean.csv'
                preview = next(row for row in csv_rows(Path(folder) / preview_file) if row[skipped_entity + '_id'] == skipped['record_id'])
                self.assertEqual(preview['phone'], skipped['before']['phone'])
                with patch.object(pipeline, 'api', side_effect=fake_api), patch.object(pipeline, 'all_records', side_effect=lambda entity: deepcopy(list(records[entity].values()))):
                    self.assertEqual(pipeline.apply_approved(), [{'name': item['name'], 'status': 'applied'}])
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0][0], selected_entity)
                    self.assertEqual(writes[0][2], {'phone': '(614) 792-9343'})
                    self.assertEqual(records[skipped_entity][skipped['record_id']]['phone'], skipped['before']['phone'])
                    repeated = pipeline.make_report(current)
                    remaining = next(proposal for proposal in repeated['proposals'] if proposal['name'] == item['name'])
                    self.assertEqual(pipeline.proposal_status(remaining, pipeline.state()), 'decided')
                    self.assertEqual(pipeline.apply_approved(), [])
                    self.assertEqual(len(writes), 1)
                website = next(row for row in current['website'] if row['name'] == item['name'])
                website['phone'] = '(234) 567-8901'
                changed = pipeline.make_report(current)
                fresh = next(proposal for proposal in changed['proposals'] if proposal['name'] == item['name'])
                self.assertEqual(pipeline.proposal_status(fresh, pipeline.state()), 'pending')

    def test_partial_selection_cannot_break_chow_or_duplicate_links(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api') as api:
            report = pipeline.make_report(self.snapshot)
            chow = next(item for item in report['proposals'] if item['category'] == 'Change of ownership')
            create = next(option for option in pipeline.approval_options(chow)
                          if chow['operations'][option['operation']]['entity'] == 'account' and option['field'] is None)
            with self.assertRaisesRegex(ValueError, 'approved together'):
                pipeline.decide(chow['id'], 'approved', chow['name'], selection=[create['key']])
            self.assertNotIn(chow['id'], pipeline.state()['decisions'])
            duplicate = next(item for item in report['proposals'] if any('duplicate_of_account' in operation['payload'] for operation in item['operations']))
            status = next(option for option in pipeline.approval_options(duplicate) if option['field'] == 'status')
            with self.assertRaisesRegex(ValueError, 'approved together'):
                pipeline.decide(duplicate['id'], 'approved', duplicate['name'], selection=[status['key']])
            with self.assertRaisesRegex(ValueError, 'at least one'):
                pipeline.decide(chow['id'], 'approved', chow['name'], selection=[])
            api.assert_not_called()

    def test_create_response_can_be_id_only_but_persisted_fields_are_verified(self):
        accounts = {row['account_id']: deepcopy(row) for row in self.snapshot['accounts']}
        contacts = {row['contact_id']: deepcopy(row) for row in self.snapshot['contacts']}
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path))
            collection = path.strip('/').split('/')[0]
            entity = collection[:-1]
            records = accounts if entity == 'account' else contacts
            if method == 'POST':
                record_id = 'CREATED_' + entity
                records[record_id] = dict(payload, **{entity + '_id': record_id})
                return {entity + '_id': record_id}
            self.assertEqual(method, 'GET')
            return deepcopy(records[path.strip('/').split('/')[1]])

        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api', side_effect=fake_api), patch.object(pipeline, 'all_records', side_effect=lambda entity: deepcopy(list((accounts if entity == 'account' else contacts).values()))):
            report = pipeline.make_report(self.snapshot)
            item = next(item for item in report['proposals'] if item['name'] == 'Bellhaven of Batavia')
            pipeline.decide(item['id'], 'approved', item['name'])
            self.assertEqual(pipeline.apply_approved(), [{'name': item['name'], 'status': 'applied'}])
            self.assertIn(('GET', '/accounts/CREATED_account'), calls)
            self.assertIn(('GET', '/contacts/CREATED_contact'), calls)
            self.assertEqual(contacts['CREATED_contact']['account_id'], 'CREATED_account')
            self.assertEqual(pipeline.apply_approved(), [])

    def test_real_create_mismatch_stops_with_exact_field_and_retry_does_not_recreate(self):
        accounts = {row['account_id']: deepcopy(row) for row in self.snapshot['accounts']}
        writes = []

        def fake_api(method, path, payload=None):
            if method == 'POST':
                writes.append(path)
                accounts['CREATED_account'] = dict(payload, account_id='CREATED_account', phone='WRONG PHONE')
                return {'account_id': 'CREATED_account'}
            self.assertEqual(method, 'GET')
            return deepcopy(accounts['CREATED_account'])

        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api', side_effect=fake_api), patch.object(pipeline, 'all_records', side_effect=lambda entity: deepcopy(list(accounts.values()) if entity == 'account' else self.snapshot['contacts'])):
            report = pipeline.make_report(self.snapshot)
            item = next(item for item in report['proposals'] if item['name'] == 'Bellhaven of Batavia')
            pipeline.decide(item['id'], 'approved', item['name'])
            result = pipeline.apply_approved()[0]
            self.assertEqual(result['status'], 'stopped')
            self.assertIn('phone', result['reason'])
            self.assertIn('WRONG PHONE', result['reason'])
            self.assertIn('CREATED_account', result['reason'])
            self.assertEqual(pipeline.apply_approved()[0]['status'], 'stopped')
            self.assertEqual(writes, ['/accounts'])
            self.assertEqual(pipeline.state()['created_ids']['NEW_ACCOUNT_973487a3636bc0f8'], 'CREATED_account')

    def test_app_partial_selection_survives_cache_clear_and_restart(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as folder, patch.object(pipeline, 'DATA', Path(folder)), patch.object(pipeline, 'api') as api:
            report = pipeline.make_report(self.snapshot)
            item = next(item for item in report['proposals'] if item['name'] == 'Bellhaven Shores of Erie')
            options = pipeline.approval_options(item)
            contact = next(option for option in options if item['operations'][option['operation']]['entity'] == 'contact')
            account = next(option for option in options if item['operations'][option['operation']]['entity'] == 'account')
            app = AppTest.from_file(pipeline.ROOT / 'app.py', default_timeout=30).run()
            app.checkbox(key='select_' + item['id'] + '_' + contact['key']).uncheck().run()
            app.button(key='approve_' + item['id']).click().run()
            self.assertFalse(app.exception)
            self.assertEqual(pipeline.state()['decisions'][item['id']]['selected'], [account['key']])
            st.cache_data.clear()
            st.cache_resource.clear()
            restarted = AppTest.from_file(pipeline.ROOT / 'app.py', default_timeout=30).run()
            restarted.radio[0].set_value('Approved').run()
            self.assertFalse(restarted.exception)
            self.assertTrue(restarted.checkbox(key='select_' + item['id'] + '_' + account['key']).value)
            self.assertFalse(restarted.checkbox(key='select_' + item['id'] + '_' + contact['key']).value)
            self.assertEqual(pipeline.state()['decisions'][item['id']]['status'], 'approved')
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
