# AI Tender Assistant

Το **AI Tender Assistant** είναι τοπική web εφαρμογή για παρακολούθηση ελληνικών δημόσιων διαγωνισμών από το ΚΗΜΔΗΣ. Ο χρήστης δημιουργεί προφίλ παρακολούθησης, η εφαρμογή εισάγει διαγωνισμούς με βάση τα CPV του προφίλ, τους βαθμολογεί ανά προφίλ και δίνει dashboard, status workflow, λεπτομέρειες, PDF analysis και αναφορές.

Η τρέχουσα έκδοση είναι **v0.9.7**. Είναι pilot-ready έκδοση με profile-oriented ροή, πλήρη τοπικό CPV tree, lazy CPV loading και καθαρότερες αναφορές.

---

## Περιεχόμενα

1. [Τι κάνει η εφαρμογή](#τι-κάνει-η-εφαρμογή)
2. [Τρέχουσα λογική χρήσης](#τρέχουσα-λογική-χρήσης)
3. [Γρήγορη εγκατάσταση με Docker](#γρήγορη-εγκατάσταση-με-docker)
4. [Ρυθμίσεις `.env`](#ρυθμίσεις-env)
5. [Προφίλ παρακολούθησης](#προφίλ-παρακολούθησης)
6. [Ingest από ΚΗΜΔΗΣ](#ingest-από-κημδησ)
7. [Dashboard](#dashboard)
8. [Scoring](#scoring)
9. [Status και νέο από εισαγωγή](#status-και-νέο-από-εισαγωγή)
10. [Λεπτομέρεια διαγωνισμού και PDF](#λεπτομέρεια-διαγωνισμού-και-pdf)
11. [Αναφορές](#αναφορές)
12. [Συντήρηση και debugging](#συντήρηση-και-debugging)
13. [Git / repository hygiene](#git--repository-hygiene)
14. [Τεχνική δομή](#τεχνική-δομή)
15. [Γνωστά όρια και επόμενα βήματα](#γνωστά-όρια-και-επόμενα-βήματα)

---

## Τι κάνει η εφαρμογή

Η εφαρμογή απαντάει στο ερώτημα:

> Ποιοι διαγωνισμοί του ΚΗΜΔΗΣ είναι πιθανές ευκαιρίες για τα προφίλ που παρακολουθούμε;

Βασική ροή:

```text
Προφίλ παρακολούθησης
→ CPV και κριτήρια προφίλ
→ ΚΗΜΔΗΣ Open Data API
→ αποθήκευση / ενημέρωση διαγωνισμών
→ βαθμολόγηση ανά προφίλ
→ dashboard / λεπτομέρειες / αναφορές
```

Η εφαρμογή είναι **metadata-first**. Δεν κατεβάζει μαζικά PDFs στο καθημερινό ingest. Το PDF αναλύεται μόνο όταν ο χρήστης το ζητήσει μέσα από τη σελίδα λεπτομέρειας ενός διαγωνισμού.

---

## Τρέχουσα λογική χρήσης

Η εφαρμογή είναι πλέον **profile-oriented**.

- Το dashboard δείχνει αποτελέσματα για το επιλεγμένο προφίλ.
- Το manual ingest από dashboard τρέχει μόνο για το επιλεγμένο προφίλ.
- Το automatic daily ingest τρέχει για όλα τα ενεργά προφίλ.
- Οι αναφορές είναι ανά προφίλ, ώστε να μην μπερδεύονται ευρήματα διαφορετικών ενδιαφερόντων.
- Ο ίδιος ΑΔΑΜ αποθηκεύεται μία φορά στη βάση, αλλά μπορεί να έχει διαφορετικό score/status ανά προφίλ.

Πρακτικά:

```text
Αυτόματο daily ingest = όλα τα ενεργά προφίλ
Χειροκίνητο ingest από dashboard = τρέχον προφίλ
Ανανέωση σχετικότητας = τρέχον προφίλ
Dashboard / reports = τρέχον ή επιλεγμένο προφίλ
```

---

## Γρήγορη εγκατάσταση με Docker

Απαραίτητα:

- Docker Desktop
- Git
- πρόσβαση internet για το ΚΗΜΔΗΣ API

Βήματα:

```powershell
cd "C:\Users\user\Documents\Python"
git clone <REPO_URL> tender_ai_assistant
cd tender_ai_assistant
copy .env.example .env
docker compose up --build -d
```

Άνοιγμα εφαρμογής:

```text
http://localhost:8000
```

Καθαρό rebuild με νέα βάση:

```powershell
docker compose down -v --rmi local --remove-orphans
docker builder prune -f
docker compose up --build -d
```

Έλεγχος logs:

```powershell
docker compose logs -f --tail=150 web
docker compose logs -f --tail=150 worker
```

---

## Ρυθμίσεις `.env`

Το `.env.example` είναι template. Για τοπική χρήση αντιγράφεται σε `.env`.

Το `.env` **δεν πρέπει να μπαίνει στο Git**, γιατί μπορεί να έχει credentials και API keys.

Βασικές ρυθμίσεις:

```env
DATABASE_URL=postgresql+psycopg://tenders:tenders@postgres:5432/tenders
KHMDHS_BASE_URL=https://cerpp.eprocurement.gov.gr
KHMDHS_TIMEOUT_SECONDS=45
KHMDHS_MAX_PAGES=100
KHMDHS_PAGE_DELAY_SECONDS=1.0
KHMDHS_RATE_LIMIT_RETRIES=4
KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS=5.0
SCHEDULE_HOUR=7
SCHEDULE_MINUTE=15
INGEST_DAYS_BACK=3
MATCH_THRESHOLD=55
APP_TIMEZONE=Europe/Athens
ADMIN_USERNAME=
ADMIN_PASSWORD=
OPENAI_API_KEY=
```

Για login στο web interface:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=change-me
```

Για αλλαγές στο `.env` χρειάζεται restart:

```powershell
docker compose restart web worker
```

---

## Προφίλ παρακολούθησης

Ένα προφίλ περιγράφει ένα ενδιαφέρον παρακολούθησης. Μπορεί να είναι πελάτης, τμήμα, κλάδος ή συγκεκριμένη εμπορική δραστηριότητα.

Περιέχει:

- CPV codes
- λέξεις που ανεβάζουν σχετικότητα
- λέξεις που μειώνουν σχετικότητα
- περιοχές NUTS
- budget range
- απαιτήσεις / πιστοποιητικά
- ενεργό / ανενεργό

### CPV

Τα CPV είναι το κύριο φίλτρο ingest. Η εφαρμογή έχει πλήρη τοπικό CPV tree και το φορτώνει lazy στη φόρμα, ώστε να μην κολλάει ο browser.

Αν δηλωθεί γονικό CPV, το ingest μπορεί να ψάξει και γνωστά παιδιά/απογόνους.

Παράδειγμα:

```text
Προφίλ: 33000000-0
Ingest: 33000000-0 + γνωστοί descendants, όπως 33140000-3, 33696500-0, 33790000-4 κλπ.
```

Προσοχή: πολύ γενικό parent CPV λειτουργεί ως broad monitoring. Δεν πρέπει να ανεβάζει αυτόματα τα αποτελέσματα σε υψηλή προτεραιότητα.

---

## Ingest από ΚΗΜΔΗΣ

### Αυτόματο daily ingest

Το worker τρέχει καθημερινά σύμφωνα με:

```env
SCHEDULE_HOUR=7
SCHEDULE_MINUTE=15
INGEST_DAYS_BACK=3
```

Το automatic ingest:

- τρέχει για όλα τα ενεργά προφίλ,
- μαζεύει τα CPV τους,
- επεκτείνει parent CPV σε descendants,
- αναζητά ΚΗΜΔΗΣ για το date window,
- αποθηκεύει νέους διαγωνισμούς,
- ενημερώνει υπάρχοντες με ίδιο ΑΔΑΜ,
- βαθμολογεί ανά προφίλ.

### Χειροκίνητο ingest από dashboard

Από το dashboard:

```text
Φίλτρα & ενέργειες → Εισαγωγή ΚΗΜΔΗΣ για αυτό το προφίλ
```

Αυτό τρέχει μόνο για το επιλεγμένο προφίλ.

### CLI ingest

Για όλα τα ενεργά προφίλ:

```powershell
docker compose run --rm web python -m app.jobs.ingest --days 3 --no-email
```

Για συγκεκριμένο profile:

```powershell
docker compose run --rm web python -m app.jobs.ingest --days 3 --profile-id 1 --no-email
```

Το `--days` αφορά ημερομηνία δημοσίευσης/καταχώρισης στο ΚΗΜΔΗΣ, όχι την καταληκτική ημερομηνία προσφορών.

---

## Dashboard

Το dashboard είναι η καθημερινή οθόνη εργασίας.

Κύρια σημεία:

- επιλογή προφίλ,
- σύνοψη του προφίλ,
- φίλτρα αποτελεσμάτων,
- γρήγορες ενέργειες,
- link για αναφορές.

Τα βασικά νούμερα αφορούν το επιλεγμένο προφίλ:

- **Ενεργά προς έλεγχο**: score ≥ threshold, όχι ληγμένα, όχι “Δεν αφορά”.
- **Υψηλής προτεραιότητας**: ενεργά/άγνωστης προθεσμίας με score ≥ 75.
- **Νέα από τελευταία εισαγωγή**: ευρήματα που συσχετίστηκαν πρώτη φορά με αυτό το προφίλ στο τελευταίο ingest.
- **Λήγουν μέσα σε 7 ημέρες**: σχετικά με κοντινή γνωστή προθεσμία.
- **Αποθηκευμένα**: όσα κράτησε χειροκίνητα ο χρήστης.

Τα ληγμένα και τα απορριφθέντα δεν χάνονται. Φαίνονται με κατάλληλα φίλτρα.

---

## Scoring

Το score είναι ανά προφίλ και ανά διαγωνισμό.

Ενδεικτική ερμηνεία:

```text
0–54    χαμηλή σχετικότητα
55–74   πιθανή ευκαιρία / μεσαία προτεραιότητα
75–100  υψηλή προτεραιότητα
```

Κριτήρια:

- CPV match
- ειδικότητα CPV
- budget
- περιοχή
- deadline
- θετικές / αρνητικές λέξεις
- PDF text / απαιτήσεις, όταν έχει γίνει ανάλυση PDF

Σημαντικό:

- Ειδικό CPV δίνει ισχυρότερο σήμα.
- Πολύ γενικό parent CPV δίνει broad monitoring, όχι αυτόματα high priority.
- Άγνωστο budget ή άγνωστη περιοχή δεν κόβει αυτόματα το score.
- Ληγμένη προθεσμία ρίχνει σημαντικά τη βαθμολογία.

Ανανέωση score μόνο για το τρέχον προφίλ:

```text
Φίλτρα & ενέργειες → Ανανέωση σχετικότητας προφίλ
```

CLI rescore όλων:

```powershell
docker compose run --rm web python -m app.jobs.rescore_tenders
```

---

## Status και νέο από εισαγωγή

Υπάρχουν δύο διαφορετικές έννοιες.

### Κατάσταση εργασίας

Είναι χειροκίνητη ενέργεια χρήστη:

- Χωρίς ενέργεια
- Αποθηκευμένο
- Σε έλεγχο
- Δεν αφορά

Αποθηκεύεται ανά προφίλ.

### Νέο από εισαγωγή

Δεν είναι status εργασίας. Είναι ένδειξη ingest.

Σημαίνει ότι το αποτέλεσμα συσχετίστηκε πρώτη φορά με το συγκεκριμένο προφίλ στο τελευταίο ingest. Αν ο ίδιος ΑΔΑΜ υπήρχε ήδη στη βάση για άλλο προφίλ, μπορεί να είναι “νέος” για το τρέχον προφίλ.

---

## Λεπτομέρεια διαγωνισμού και PDF

Στη σελίδα λεπτομέρειας φαίνονται:

- στοιχεία ΚΗΜΔΗΣ,
- ημερομηνίες,
- CPV,
- λόγοι βαθμολόγησης,
- status,
- σύνδεσμος επίσημου PDF,
- Ανάλυση PDF.

Το επίσημο PDF εμφανίζεται εδώ, όχι ως κουμπί στις κάρτες του dashboard, για να μείνει το dashboard καθαρό.

Η Ανάλυση PDF:

- κατεβάζει το PDF,
- εξάγει κείμενο όπου είναι δυνατό,
- ενημερώνει το score με βάση το κείμενο,
- δεν κάνει OCR σε scanned PDFs.

---

## Αναφορές

Οι αναφορές είναι ανά προφίλ.

Βασικές επιλογές περιεχομένου:

- **Πιθανές ευκαιρίες**: σχετικά ενεργά ευρήματα για πελάτη ή εβδομαδιαία ενημέρωση.
- **Νέα από τελευταία εισαγωγή**: τι εμφανίστηκε στο τελευταίο ingest για το προφίλ.
- **Αποθηκευμένα / σε έλεγχο**: χειροκίνητη shortlist χρήστη.
- **Όλα τα μη απορριφθέντα**: πλήρης εικόνα χωρίς “Δεν αφορά”.

Βασικά φίλτρα:

- Περίοδος ΚΗΜΔΗΣ από/έως
- Προφίλ
- Περιεχόμενο αναφοράς

Προαιρετικά φίλτρα:

- ελάχιστο score,
- περιοχή NUTS,
- αναζήτηση,
- μόνο ενεργά ή άγνωστης προθεσμίας.

Exports:

- **PDF αναφοράς** για πελάτη ή εσωτερική ενημέρωση.
- **CSV** για Excel / περαιτέρω επεξεργασία.
- **PDF προφίλ** για περιγραφή του προφίλ.
- JSONL / Markdown υπάρχουν ως τεχνικές εξαγωγές.

---

## Συντήρηση και debugging

Η τεχνική σελίδα συντήρησης είναι κρυφή από το βασικό μενού:

```text
/maintenance
```

Δείχνει συνοπτικά μεγέθη βάσης, ingest info και ρυθμίσεις.

Χρήσιμα checks:

```powershell
docker compose exec postgres psql -U tenders -d tenders -c "select pg_size_pretty(pg_database_size('tenders')) as db_size;"
```

```powershell
docker compose exec postgres psql -U tenders -d tenders -c "select count(*) as tenders from tenders; select count(*) as scores from tender_scores; select count(*) as profiles from client_profiles;"
```

```powershell
docker compose exec postgres psql -P pager=off -U tenders -d tenders -c "select p.name, count(ts.id) as scores, count(*) filter (where ts.score >= 55) as matches, count(*) filter (where ts.score >= 75) as high from client_profiles p left join tender_scores ts on ts.profile_id = p.id group by p.name order by p.name;"
```

Logs:

```powershell
docker compose logs --tail=150 web
docker compose logs --tail=150 worker
```

Run tests locally:

```powershell
docker compose run --rm web pytest -q
```

or without Docker, if dependencies are installed:

```bash
PYTHONPATH=. pytest -q
```

---

## Git / repository hygiene

Το repo πρέπει να περιέχει κώδικα, templates, tests, configuration examples και στατικά reference data.

Να μπαίνουν στο Git:

```text
app/
config/cpv_catalog_full.json
config/regions_nuts.yml
config/profiles.yml, αν θέλετε να κρατηθεί ως sample/reference
tests/
Dockerfile
docker-compose.yml
requirements.txt
.env.example
.gitignore
.dockerignore
README.md
```

Να μη μπαίνουν στο Git:

```text
.env
.env.local
OpenAI keys / passwords
PostgreSQL volumes ή dumps
παραγόμενα PDFs / CSVs / reports
zip patches
__pycache__
.pytest_cache
logs
τοπικά venvs
```

### Πρώτο ανέβασμα σε νέο repo

```powershell
cd "C:\Users\user\Documents\Python\tender_ai_assistant v2"
git init
git status
```

Έλεγξε ότι δεν εμφανίζεται `.env`.

Πρόσθεσε τα αρχεία:

```powershell
git add .
git status
```

Αν δεις κάτι που δεν πρέπει να ανέβει, αφαίρεσέ το πριν το commit:

```powershell
git restore --staged <file>
```

Commit:

```powershell
git commit -m "Initial AI Tender Assistant v0.9.7"
```

Σύνδεση remote:

```powershell
git branch -M main
git remote add origin <REPO_URL>
git push -u origin main
```

Για έλεγχο πριν το push:

```powershell
git status --ignored
git ls-files
```

---

## Τεχνική δομή

```text
app/main.py                 FastAPI routes και UI orchestration
app/models.py               SQLAlchemy models
app/db.py                   DB session / engine
app/config.py               Ρυθμίσεις από .env
app/jobs/ingest.py          Manual/automatic ingest από ΚΗΜΔΗΣ
app/jobs/scheduler.py       Daily worker
app/jobs/rescore_tenders.py CLI rescore
app/services/khmdhs_client.py ΚΗΜΔΗΣ API client
app/services/scoring.py     Rule-based scoring
app/services/cpv_catalog.py CPV tree / descendants
app/services/reports.py     PDF/CSV/JSONL/Markdown reports
app/services/pdf.py         PDF text extraction
app/templates/              Jinja templates
config/                     CPV και NUTS reference data
tests/                      pytest suite
```

Database tables σε υψηλό επίπεδο:

- `tenders`: κοινή αποθήκη διαγωνισμών.
- `tender_scores`: score/status/reasons ανά διαγωνισμό και προφίλ.
- `client_profiles`: προφίλ παρακολούθησης.
- `system_events`: ingest/rescore/system activity.

---

## Γνωστά όρια και επόμενα βήματα

Τρέχοντες συνειδητοί περιορισμοί:

- Δεν γίνεται OCR σε scanned PDFs.
- Δεν γίνεται μαζικό PDF download στο ingest.
- Η περιοχή χρησιμοποιείται κυρίως στο scoring, όχι ως αυστηρό ingest filter.
- Το OpenAI είναι προαιρετικό και δεν αποτελεί βασική απόφαση scoring.
- Το Diavgeia δεν είναι ακόμα πλήρως ενσωματωμένο ως δεύτερη πηγή.

Προτεινόμενα επόμενα βήματα:

1. QA με καθαρή βάση και 2–3 πραγματικά profiles.
2. Έλεγχος PDF/CSV exports σε πραγματικές αναφορές.
3. Καλύτερη ορατότητα τελευταίου ingest σε activity/maintenance.
4. Προαιρετικό Diavgeia connector ως χωριστή πηγή, όχι merge με ΚΗΜΔΗΣ.
5. AI-assisted summaries/checklists σε επίπεδο λεπτομέρειας, όχι AI ως κύρια βαθμολογία.
