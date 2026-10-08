# Bellhaven daily review — simple demo

Three Python files do the work: `pipeline.py` downloads and compares sources, `matching.py` prepares corrections, and `app.py` presents the review screen. This folder is independent of FINAL.

## Open and use

1. Double-click **START_REVIEW.cmd**. The first launch installs the dependencies; later launches open the app directly.
2. Open http://localhost:8501 if the browser does not open automatically.
3. Click **Refresh today's data**. It downloads every page of CRM accounts and contacts, and every website community detail page, including the separately discovered Findlay page.
4. In **Changes to approve**, read each facility's website/CRM evidence and before/after table. Check only the changes you want and click **Approve selected changes**, or reject the proposal. For example, approve the account phone while leaving the contact phone unchecked. Unchecked changes are declined, not queued for automatic approval later. New records are selected as a whole; related CHOW and duplicate changes cannot be split unsafely.
5. Click **Apply approved corrections to CRM** when ready. Only the approved proposals are sent to the API. A rejection, refresh, or daily comparison never writes to CRM.

The token is read from the original project `.env`, a `.env` in this folder, or `BH_API_TOKEN` / `API_TOKEN` in the environment. Do not put the token in GitHub YAML.

## What happens each day

- All three raw sources are saved in `data/`: `crm_raw.csv`, `contacts_raw.csv`, and `communities.csv`.
- `data/snapshots/YYYY-MM-DD.json` keeps the complete raw sources for each day. The latest refresh replaces today's snapshot; it does not replace yesterday's snapshot.
- **Daily comparison** shows website/account/contact records that were added, removed, or changed since the most recent earlier saved calendar day. It also counts unchanged records. Timestamp-only edits are ignored.
- On the first day there is no invented yesterday: the first download becomes the baseline. Current website/CRM mismatches still go to review immediately.
- **Changes to approve** proposes current corrections even if the sources have not changed since yesterday. Correct source changes that need no CRM edit are shown only in the daily comparison.
- `data/review.json` remembers approvals, rejections, investigated identities and created API IDs. Already decided items leave the pending queue. A materially different correction needs a fresh decision.
- Partial approvals remember each selected/declined change, even after applying selected fields and refreshing. **Decision history** shows how many changes were selected. Streamlit's **Clear cache** does not delete saved decisions; it may reset unsaved checkbox choices. Keep `data/review.json` to preserve decisions.
- Daily refresh only reads and prepares proposals. The reviewer controls API writes.

## Matching and corrections

Website fields are authoritative for a verified facility: name, address, ZIP, first care offering, administrator, and published contact phone. Match using normalized name/address plus phone and administrator evidence. Search the entire CRM, including other parents. Uncertain candidates go to the app for identity confirmation rather than being silently changed.

`matching_choices.csv` keeps the identities investigated earlier, including the separate Union Square facilities. `duplicates.csv` keeps the seven investigated duplicate relationships. They are readable reference data, not approvals to write. New identities can be confirmed in the app.

When revenue history and outstanding AR are both positive, CHOW proposals preserve the old account's name, parent, billing fields, status and note. Only its `chow_current_account` link changes; a new account and contact are created under the correct parent. Otherwise the existing account can be reparented. Different administrator people receive separate contacts; old names/emails are preserved and old administrator roles are deactivated. Duplicate accounts are retained, marked Inactive and linked to the survivor. Unlisted Bellhaven accounts receive Needs Review and the agreed closed/switched-parent note.

`data/crm_clean.csv` and `data/contacts_clean.csv` are proposed exports. NEW_ identifiers are placeholders until API creation returns real IDs. The app checks current CRM values again before writing, then verifies persisted fields with a separate GET request, including after creating a record. An incomplete create response is not treated as proof that fields failed to save. Actual read-back mismatches identify the record and exact fields and stop the batch. If sources changed after review, refresh and review the revised proposal. Interrupted creates stop for inspection rather than automatically posting a second record.

## Daily schedule example

`.github/workflows/daily.yml` is an example, not an activated schedule. It runs at 10:00 UTC (06:00 Eastern daylight time / 05:00 Eastern standard time).

It uses a Windows self-hosted runner on the same computer as the app, so daily snapshots and review decisions remain in the same folder. For a demo, no runner setup is needed: the YAML simply shows the schedule you would use. To activate later, copy it into the repository's root `.github/workflows/`, configure a Windows runner, set repository variable `BELLHAVEN_DEMO_FOLDER` to this folder's absolute path, and optionally set the `BH_API_TOKEN` secret. Run START_REVIEW.cmd once first to create the environment.

For a manual daily run, use `.venv\Scripts\python.exe pipeline.py`. To rebuild without network calls from the existing FINAL exports, use `.venv\Scripts\python.exe pipeline.py --offline ..\FINAL`.

Keep the `data` folder between runs. Do not delete `review.json` if you want previous decisions preserved. Existing notebooks, documents and FINAL files remain in their original locations.

## Demo checks

Run `.venv\Scripts\python.exe check_demo.py` to check source completeness, prior-day comparison, approval persistence, partial approvals and cache clearing, CHOW/duplicates, simulated API application and the app's Reject button. These checks use the original-source fixture in `test_data/source_snapshot.json` and temporary review data, so applying real CRM changes does not change the test inputs. They do not write to the live CRM. Include `test_data` if you want to run the checks from your ZIP or GitHub submission.
