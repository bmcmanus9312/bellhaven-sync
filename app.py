import streamlit as st

import pipeline


st.set_page_config(page_title='Bellhaven daily review', page_icon='🏠', layout='wide')
st.title('Bellhaven daily review')
st.caption('Refresh the data, review differences, then apply the corrections you approved.')


def run_action(action):
    try:
        with st.spinner('Working…'):
            action()
        st.rerun()
    except Exception as error:
        st.error(str(error))


def show_record(label, row):
    st.markdown('**' + label + '**')
    if not row:
        st.write('Not listed on the website.' if label == 'Website' else 'No existing CRM record selected.')
        return
    st.write(row.get('name', ''))
    st.write(', '.join(str(row.get(field, '')) for field in ('street_address', 'city', 'state', 'zip') if row.get(field)) or ', '.join(str(row.get(field, '')) for field in ('billing_street', 'billing_city', 'billing_state', 'billing_zip') if row.get(field)))
    st.write('Phone: ' + str(row.get('phone') or 'Missing'))
    if 'administrator' in row:
        st.write('Administrator: ' + (row.get('administrator') or 'Missing'))
        st.write('Care: ' + row.get('care_offerings', ''))
        st.link_button('Open website page', row['url'])
    else:
        st.write('Parent: ' + str(row.get('parent_name') or 'None'))
        st.write('Revenue: ' + str(row.get('lifetime_revenue', '')) + ' · Outstanding AR: ' + str(row.get('outstanding_ar', '')))
        st.caption('CRM ID: ' + row.get('account_id', ''))


if st.button('Refresh today’s data', type='primary'):
    run_action(pipeline.refresh)

report = pipeline.read_json(pipeline.DATA / 'report.json')
if report is None:
    st.info('Click Refresh today’s data to download all website locations, CRM accounts and contacts.')
    st.stop()

saved = pipeline.state()
pending = [item for item in report['proposals'] if pipeline.proposal_status(item, saved) == 'pending']
approved = [item for item in report['proposals'] if saved['decisions'].get(item['id'], {}).get('status') == 'approved' and item['operations']]
columns = st.columns(4)
for column, label, value in zip(columns, ['Website locations', 'CRM accounts', 'CRM contacts', 'Awaiting review'], [report['counts']['website'], report['counts']['accounts'], report['counts']['contacts'], len(pending)]):
    column.metric(label, value)
st.caption('Last downloaded: ' + report['fetched_at'])

if st.button(f'Apply {len(approved)} approved corrections to CRM', disabled=not approved):
    def apply():
        results = pipeline.apply_approved()
        errors = [item for item in results if item['status'] == 'stopped']
        if errors:
            st.session_state['apply_message'] = errors[0]['name'] + ': ' + errors[0]['reason']
        else:
            st.session_state['apply_message'] = f'Applied {len(results)} approved corrections.'
        pipeline.refresh()
    run_action(apply)
if st.session_state.get('apply_message'):
    st.info(st.session_state['apply_message'])

review_tab, daily_tab, coverage_tab, history_tab = st.tabs(['Changes to approve', 'Daily comparison', 'Community matches', 'Decision history'])
with review_tab:
    view = st.radio('Show', ['Awaiting review', 'Approved'], horizontal=True)
    items = pending if view == 'Awaiting review' else approved
    if not items:
        st.success('No corrections in this view.')
    for item in items:
        with st.expander(item['name'] + ' — ' + item['category']):
            st.write(item['reason'])
            evidence = item['evidence']
            left, right = st.columns(2)
            with left:
                show_record('Website', evidence.get('website'))
            with right:
                show_record('CRM', evidence.get('crm'))
            rows = [{'Record': operation['entity'] + ' ' + operation['action'], 'Field': field,
                     'Before': str(value['before']), 'Proposed': str(value['after'])}
                    for operation in item['operations'] for field, value in operation['changes'].items()]
            if rows:
                st.dataframe(rows, hide_index=True, width='stretch')
            selection = []
            if item['operations']:
                st.markdown('**Select the changes to approve**')
                st.caption('Click Approve selected changes to save your selection before applying. Unchecked changes will be declined and left unchanged in CRM. New records are approved as a whole; CHOW links and duplicate flags must stay together.')
                defaults = set(pipeline.selected_keys(item, saved))
                for option in pipeline.approval_options(item):
                    operation = item['operations'][option['operation']]
                    if option['field'] is None:
                        label = 'Create ' + operation['entity'] + ': ' + operation['payload'].get('name', '') + ' (all fields)'
                    else:
                        change = operation['changes'][option['field']]
                        label = operation['entity'].capitalize() + ' ' + option['field'] + ': ' + str(change['before']) + ' → ' + str(change['after'])
                    help_text = 'The website phone may be the facility line, not the administrator’s direct number.' if operation['entity'] == 'contact' and option['field'] == 'phone' else None
                    if st.checkbox(label, value=option['key'] in defaults, key='select_' + item['id'] + '_' + option['key'], help=help_text):
                        selection.append(option['key'])
            if evidence.get('survivor'):
                show_record('Duplicate survivor', evidence['survivor'])
            if not item['operations'] and evidence.get('website') and evidence.get('candidates'):
                options = {candidate['account']['name'] + ' | ' + candidate['account']['account_id']: candidate['account']['account_id'] for candidate in evidence['candidates']}
                st.dataframe([{'CRM account': candidate['account']['name'], 'ID': candidate['account']['account_id'],
                               'Address': candidate['account'].get('billing_street'), 'Parent': candidate['account'].get('parent_name'),
                               'Address agrees': candidate['same_address'], 'Phone agrees': candidate['same_phone'],
                               'Administrator agrees': candidate['same_administrator']} for candidate in evidence['candidates']], hide_index=True)
                options['A separate facility — propose a new account'] = 'new'
                selected = st.selectbox('Which facility is this?', list(options), index=None, key='match_' + item['id'], placeholder='Choose after checking the evidence')
                if st.button('Confirm facility identity', key='confirm_' + item['id'], disabled=selected is None):
                    run_action(lambda: pipeline.confirm_match(evidence['website']['url'], options[selected]))
            note = st.text_input('Optional reviewer note', value=saved['decisions'].get(item['id'], {}).get('note', ''), key='note_' + item['id'])
            first, second = st.columns(2)
            if first.button('Approve selected changes' if item['operations'] else 'Mark reviewed', key='approve_' + item['id'], disabled=bool(item['operations']) and not selection):
                run_action(lambda: pipeline.decide(item['id'], 'approved' if item['operations'] else 'acknowledged', item['name'], note, selection))
            if second.button('Reject', key='reject_' + item['id']):
                run_action(lambda: pipeline.decide(item['id'], 'rejected', item['name'], note))

with daily_tab:
    if not report['previous_date']:
        st.info('First day: today’s sources are the baseline. On the next day, this tab compares against the most recent earlier saved day. The correction queue already compares today’s website with CRM.')
    else:
        st.write('Compared with ' + report['previous_date'] + '. Timestamp-only updates are excluded.')
        changes = report['source_changes']
        earlier = pipeline.read_json(pipeline.DATA / 'snapshots' / (report['previous_date'] + '.json'))
        latest = pipeline.read_json(pipeline.DATA / 'latest.json')
        for source, identity, label in [('website', 'url', 'Website'), ('accounts', 'account_id', 'CRM accounts'), ('contacts', 'contact_id', 'CRM contacts')]:
            changed_ids = {item['record_id'] for item in changes if item['source'] == source}
            unchanged = len({row[identity] for row in earlier[source]} & {row[identity] for row in latest[source]} - changed_ids)
            st.write(f'{label}: {unchanged} unchanged records; {len(changed_ids)} added, removed or changed records.')
        include_reviewed = st.checkbox('Include differences already reviewed')
        shown = [item for item in changes if include_reviewed or item['id'] not in saved['decisions']]
        if shown:
            st.dataframe([{'Source': item['source'], 'Name': item['name'], 'Change': item['change'], 'Field': item['field'], 'Previous day': str(item['previous']), 'Today': str(item['current'])} for item in shown], hide_index=True, width='stretch')
            st.caption('Corrections requiring CRM edits appear in Changes to approve. Reviewing this comparison does not modify CRM.')
            if st.button('Mark displayed differences reviewed'):
                def acknowledge():
                    for item in shown:
                        pipeline.decide(item['id'], 'acknowledged', item['name'] + ': ' + item['field'])
                run_action(acknowledge)
        else:
            st.success('No unreviewed daily differences.')

with coverage_tab:
    st.dataframe(report['matches'], hide_index=True, width='stretch')
    st.caption('Care type uses the first website offering. Website administrators and published contact phone numbers are also compared.')

with history_tab:
    history = [{'Name': item['name'], 'Decision': item['status'],
                'Selected': str(item['selected_count']) + ' of ' + str(item['total_count']) if 'selected_count' in item else 'Whole proposal',
                'Note': item.get('note', ''), 'When': item['decided_at']} for item in saved['decisions'].values()]
    if history:
        st.dataframe(history, hide_index=True, width='stretch')
    else:
        st.write('No decisions yet. Decisions are saved automatically and retained on future runs.')
