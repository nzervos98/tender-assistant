# Tender Assistant — Τεχνική Έκθεση Συστήματος

**Έκδοση τεκμηρίωσης:** v0.10.6  
**Έκδοση εφαρμογής βάσης:** v0.10.6  
**Τύπος συστήματος:** τοπική web εφαρμογή παρακολούθησης και αξιολόγησης δημόσιων προμηθειών  
**Κύριες πηγές δεδομένων:** ΚΗΜΔΗΣ OpenData API, Διαύγεια OpenData API

Το **Tender Assistant** είναι εφαρμογή υποστήριξης παρακολούθησης ελληνικών δημόσιων προμηθειών. Το σύστημα συλλέγει πράξεις από το ΚΗΜΔΗΣ, τις κανονικοποιεί σε ενιαίο μοντέλο δεδομένων, τις συσχετίζει με προφίλ ενδιαφέροντος, υπολογίζει rule-based βαθμολογία σχετικότητας ανά προφίλ και παρέχει dashboard, αναφορές, workflow αξιολόγησης, ανάλυση PDF και εμπλουτισμό από τη Διαύγεια.

Η εφαρμογή είναι **profile-oriented**: ένας διαγωνισμός αποθηκεύεται μία φορά στον πίνακα `tenders`, αλλά μπορεί να έχει διαφορετική αξιολόγηση, κατάσταση εργασίας και ένδειξη νέου ευρήματος ανά προφίλ στον πίνακα `tender_scores`.

---

## 1. Πεδίο εφαρμογής

Το σύστημα καλύπτει τις παρακάτω λειτουργικές ενότητες:

| Ενότητα | Περιγραφή |
|---|---|
| Προφίλ παρακολούθησης | Ορισμός CPV, περιοχών NUTS, budget και απαιτήσεων. |
| Εισαγωγή ΚΗΜΔΗΣ | Ανάκτηση πράξεων από το ΚΗΜΔΗΣ OpenData API, κυρίως από τις Προσκλήσεις/Προκηρύξεις/Διακηρύξεις. |
| Γενική Αναζήτηση ΚΗΜΔΗΣ | Live αναζήτηση σε πολλαπλά ΚΗΜΔΗΣ resources για ad hoc έλεγχο και profile-specific αποθήκευση. |
| Scoring | Rule-based αξιολόγηση σχετικότητας ανά προφίλ, με βάση CPV, απαιτήσεις, περιοχές και budget. Η λήξη/ακύρωση είναι ξεχωριστή κατάσταση. |
| Dashboard | Επισκόπηση ευρημάτων ανά προφίλ με σελιδοποίηση 20 εγγραφών, σταθερό φίλτρο Ακριβές/Μερικό/Child CPV match, ακριβές εύρος λήξης, status, score, αναζήτηση και γεωγραφικά φίλτρα. |
| Έλεγχος απαιτήσεων PDF | On-demand λήψη, εξαγωγή ενσωματωμένου κειμένου και OCR fallback για σαρωμένα PDF ΚΗΜΔΗΣ. |
| Διαύγεια enrichment | Read-only αναζήτηση σχετικών πράξεων Διαύγειας με βάση τον ΑΔΑΜ και εμφάνιση structured metadata ως επικουρική τεκμηρίωση. |
| Reports | Εξαγωγές PDF, CSV, JSONL και Markdown ανά προφίλ και φίλτρα. |
| Συντήρηση | Activity log, maintenance page και system events. |

---

## 2. Αρχιτεκτονική υψηλού επιπέδου

Η εφαρμογή αποτελείται από FastAPI web application, PostgreSQL database και worker/scheduler container.

```text
External APIs
  ├─ ΚΗΜΔΗΣ OpenData API
  └─ Διαύγεια OpenData API
        ↓
FastAPI application
  ├─ UI routes
  ├─ API clients
  ├─ scoring services
  ├─ report services
  └─ PDF extraction
        ↓
PostgreSQL
  ├─ tenders
  ├─ tender_scores
  ├─ client_profiles
  ├─ diavgeia_decisions
  └─ system_events
```

Η βασική σχεδιαστική επιλογή είναι **metadata-first ingest**. Το καθημερινό ingest αποθηκεύει μεταδεδομένα και επίσημους συνδέσμους PDF, αλλά δεν κατεβάζει μαζικά τα PDF. Η ανάλυση PDF πραγματοποιείται on demand από τη σελίδα λεπτομέρειας διαγωνισμού ή προαιρετικά μέσω ρητής ρύθμισης.

---

## 3. Εξωτερικά API

### 3.1 ΚΗΜΔΗΣ OpenData API

**Επίσημο reference:** `https://cerpp.eprocurement.gov.gr/khmdhs-opendata/help`  
**Base URL:** `https://cerpp.eprocurement.gov.gr`  
**Μορφή ανταλλαγής:** JSON για OpenData search calls.  
**Rate limit:** 350 αιτήματα ανά λεπτό για το OpenData API. Σε υπέρβαση επιστρέφεται HTTP `429 Too Many Requests`.  
**Ενημέρωση OpenData:** τα δεδομένα ενημερώνονται περίπου κάθε 24 ώρες από το ΚΗΜΔΗΣ.

Το σύστημα χρησιμοποιεί τα παρακάτω ΚΗΜΔΗΣ resources:

| Resource | Endpoint | Ρόλος στο σύστημα |
|---|---|---|
| `notice` | `POST /khmdhs-opendata/notice?page=N` | Προσκλήσεις, Προκηρύξεις και Διακηρύξεις. Κύριο source για ευκαιρίες συμμετοχής. |
| `request` | `POST /khmdhs-opendata/request?page=N` | Αιτήματα. Χρησιμοποιούνται στη Γενική Αναζήτηση ως πρώιμα σήματα. |
| Πορεία υπόθεσης | `POST /khmdhs-opendata/request` και, όταν δεν υπάρχει συνδεδεμένο `REQ`, `GET /khmdhs-opendata/adamChain/{referenceNumber}` | Η σελίδα λεπτομέρειας ανακτά κατά προτεραιότητα τη δομημένη εγγραφή του συνδεδεμένου αιτήματος, με όριο αναμονής 4 δευτερολέπτων, και χρησιμοποιεί αθόρυβα τις ήδη αποθηκευμένες συνδέσεις ως ασφαλές fallback. |
| `attachment` | `GET /khmdhs-opendata/{resource}/attachment/{referenceNumber}` | Επίσημο PDF πράξης. Χρησιμοποιείται για on-demand PDF analysis. |

Το κύριο ingest χρησιμοποιεί μόνο το `notice`. Η Γενική Αναζήτηση ΚΗΜΔΗΣ υποστηρίζει `notice` και `request`.

### 3.2 Διαύγεια OpenData API

**Επίσημο reference:** `https://diavgeia.gov.gr/api/help`  
**Προεπιλεγμένο base URL εφαρμογής:** `https://diavgeia.gov.gr/luminapi/opendata`

Η Διαύγεια χρησιμοποιείται ως **read-only enrichment layer**. Δεν αντικαθιστά το ΚΗΜΔΗΣ ως source ευκαιριών. Ο ρόλος της είναι η τεκμηρίωση σχετικών διοικητικών πράξεων γύρω από έναν διαγωνισμό, όπως προσκλήσεις, αποφάσεις, αναθέσεις, συμβάσεις, πληρωμές, σχετικοί ΑΔΑ και δομημένα πεδία οικονομικού/CPV ενδιαφέροντος.

Βασική θέση συστήματος:

```text
ΚΗΜΔΗΣ = primary source για ευκαιρίες και διαγωνισμούς
Διαύγεια = secondary evidence / enrichment για διοικητικό context
```

Στο τρέχον στάδιο η Διαύγεια δεν συμμετέχει στο score. Τα αποτελέσματά της εμφανίζονται στη λεπτομέρεια διαγωνισμού ως τεκμηρίωση.

Η λειτουργία Διαύγειας ορίζεται με πέντε κανόνες προϊόντος:

1. Παραμένει panel τεκμηρίωσης στη λεπτομέρεια διαγωνισμού.
2. Εμφανίζει readable labels όπου αυτά επιστρέφονται από το API και διατηρεί IDs όταν δεν επιστρέφονται labels.
3. Όταν δεν υπάρχει ΑΔΑΜ match, εμφανίζει ρητό μήνυμα ότι δεν βρέθηκε ασφαλές exact match, όχι ότι δεν υπάρχει διοικητικό ιστορικό.
4. Δεν εκτελεί aggressive fallback auto-save με τίτλο/CPV/φορέα, ώστε να αποφεύγονται false positives.
5. Η τεκμηρίωση και το UI παρουσιάζουν τη Διαύγεια ως secondary evidence layer και το ΚΗΜΔΗΣ ως primary source ευκαιριών και πρώιμων σημάτων.

---

## 4. Χειρισμός NUTS και περιοχών

Το ΚΗΜΔΗΣ OpenData search για `notice` δεν παρέχει, στην τεκμηριωμένη request body μορφή, επίσημο φίλτρο τύπου `nutsCode` ή `nutsCodes` για περιορισμό των αποτελεσμάτων στο API request. Η τεκμηρίωση περιλαμβάνει πεδία όπως `title`, `cpvItems`, `organizations`, `signer`, `contractType`, `dateFrom`, `dateTo`, `totalCostFrom`, `totalCostTo`, `referenceNumber`, `procedureType`, `finalDateFrom`, `finalDateTo`, `aaht`, `publicFundingRefNum` και `isModified`, αλλά όχι NUTS search parameter.

Τα δύο βασικά NUTS πεδία έχουν διαφορετική επίσημη σημασία: το `nutsCode` περιγράφει τη γεωγραφική περιοχή της Αναθέτουσας Αρχής, ενώ το `nutsCodes` είναι λίστα με τον τόπο ή τους τόπους εκτέλεσης της σύμβασης. Τα `nutsCity`, `nutsPostalCode` και `nutsCountry` ανήκουν επίσης στα στοιχεία διεύθυνσης του φορέα. Η εφαρμογή τα αξιοποιεί ως **local scoring/filtering signals** και όχι ως upstream API φίλτρα.

Πρακτική συνέπεια:

```text
Το ΚΗΜΔΗΣ API περιορίζεται με CPV, ημερομηνίες, φορείς, ποσά και ΑΔΑΜ.
Οι περιοχές NUTS αξιολογούνται μετά την ανάκτηση, μέσα στην εφαρμογή.
```

Το dashboard και οι αναφορές προσφέρουν δύο ανεξάρτητα φίλτρα: «Τόπος εκτέλεσης NUTS» (`nutsCodes`) και «Έδρα Αναθέτουσας Αρχής NUTS» (`nutsCode`). Η βαθμολόγηση περιοχής χρησιμοποιεί κατά προτεραιότητα τον δηλωμένο τόπο εκτέλεσης. Η έδρα του φορέα χρησιμοποιείται μόνο ως ασθενέστερο fallback όταν δεν έχει δηλωθεί `nutsCodes`, ώστε ένας φορέας με έδρα στην Αττική να μη χαρακτηρίζει ως αττικό ένα έργο που εκτελείται στη Θήρα.

---

## 5. Κλήσεις ΚΗΜΔΗΣ στην εφαρμογή

### 5.1 Γενική Αναζήτηση ΚΗΜΔΗΣ

Η οθόνη `/kimdis` είναι live search εργαλείο. Δεν αποτελεί το καθημερινό ingest. Σκοπός της είναι ο ad hoc έλεγχος του επίσημου API, η διερεύνηση συγκεκριμένων ΑΔΑΜ και η χειροκίνητη αποθήκευση αποτελεσμάτων στο επιλεγμένο προφίλ.

Τα views της οθόνης αντιστοιχούν στα παρακάτω resources:

| View | Resources | Περιγραφή |
|---|---|---|
| `opportunities` | `notice` | Ευκαιρίες συμμετοχής. Διακηρύξεις/προσκλήσεις με πιθανό ενδιαφέρον συμμετοχής. |
| `signals` | `request` | Πρώιμα σήματα πιθανής μελλοντικής ανάγκης. |
| `advanced` | `notice`, `request` ή και τα δύο | Τεχνική αναζήτηση στα υποστηριζόμενα είδη πράξης. |

Το request body δημιουργείται δυναμικά μέσω `build_search_body()`. Ενδεικτικά πεδία:

```json
{
  "isModified": false,
  "title": "...",
  "referenceNumber": "...",
  "cpvItems": ["33790000-4"],
  "organizations": ["100015981"],
  "contractType": "13",
  "procedureType": "...",
  "dateFrom": "2026-06-01",
  "dateTo": "2026-06-22",
  "totalCostFrom": 0,
  "totalCostTo": 10000,
  "finalDateFrom": "2026-06-22 00:00",
  "finalDateTo": "2026-06-30 23:59"
}
```

Δεν αποστέλλονται όλα τα πεδία σε όλα τα resources. Το `isModified`, το `procedureType` και τα `finalDateFrom`/`finalDateTo` εφαρμόζονται μόνο στο `notice`, επειδή το `request` δεν υποστηρίζει όλα τα ίδια φίλτρα.

Αν ο χρήστης δώσει ΑΔΑΜ, το σύστημα κάνει infer το resource:

| Περιεχόμενο ΑΔΑΜ | Resource |
|---|---|
| `REQ` | `request` |
| `PROC` | `notice` |

Οι ιστορικοί τύποι `AWRD`, `SYMV` και `PAY` δεν αναζητούνται πλέον, επειδή το Market Intelligence feature έχει αφαιρεθεί.

Σε αναζήτηση με ΑΔΑΜ, τα φίλτρα ημερομηνίας και ενεργών πράξεων αγνοούνται ώστε να μην αποκλειστεί ακριβές αποτέλεσμα από στενό date window.

Η Γενική Αναζήτηση έχει UI safety caps:

| Περιορισμός | Τιμή |
|---|---:|
| Default `max_pages` | 1 |
| Μέγιστο `max_pages` από UI | 5 |
| Μέγιστο πλήθος εμφανιζόμενων αποτελεσμάτων | 300 |

Από v0.10.2, η αποθήκευση από τη Γενική Αναζήτηση είναι **profile-specific**. Το `/kimdis/save` απαιτεί `profile_id`, αποθηκεύει ή ενημερώνει το tender και δημιουργεί score μόνο για το συγκεκριμένο προφίλ. Από v0.10.5, επειδή πρόκειται για ρητή χειροκίνητη ενέργεια χρήστη, το αντίστοιχο score λαμβάνει αυτόματα `user_status = saved`.

### 5.2 Κανονικό ingest ΚΗΜΔΗΣ

Το κανονικό ingest εκτελείται από το dashboard, το CLI ή τον scheduler. Σκοπός του είναι η παραγωγική παρακολούθηση ευκαιριών.

Το ingest χρησιμοποιεί αποκλειστικά:

```text
POST /khmdhs-opendata/notice?page=N
```

Το request body αποτελείται κυρίως από:

```json
{
  "isModified": false,
  "dateFrom": "today - INGEST_DAYS_BACK",
  "dateTo": "today",
  "cpvItems": ["selected CPV", "known descendants"]
}
```

Το ingest δεν αποστέλλει στο ΚΗΜΔΗΣ:

- NUTS / preferred regions,
- budget προφίλ,
- required certificates,
- active-only deadline filter,
- organization text fallback.

Τα παραπάνω εφαρμόζονται μετά την αποθήκευση ως scoring και local filtering λογική.

### 5.3 CPV expansion

Τα CPV των ενεργών προφίλ συλλέγονται και επεκτείνονται με γνωστούς απογόνους από τον τοπικό πλήρη CPV κατάλογο (`config/cpv_catalog_full.json`). Αυτό επιτρέπει σε parent CPV, όπως `33000000-0`, να καλύπτουν γνωστά child/descendant CPV όπως `33790000-4`, εφόσον αυτά υπάρχουν στον κατάλογο.

Η επέκταση CPV επηρεάζει το upstream API call, επειδή οι descendants αποστέλλονται στο `cpvItems`. Δεν αυξάνει τον αριθμό HTTP requests ανά CPV, καθώς τα CPV περιλαμβάνονται στο ίδιο request body και η σελιδοποίηση γίνεται με `page=N`.

---

## 6. Περιορισμοί εισαγωγής και επίδραση στις επιστροφές API

Η εφαρμογή περιορίζει τα αποτελέσματα του ΚΗΜΔΗΣ με συνδυασμό upstream και local περιορισμών.

### 6.1 Upstream περιορισμοί

| Περιορισμός | Πού εφαρμόζεται | Επίδραση |
|---|---|---|
| `dateFrom` / `dateTo` | ΚΗΜΔΗΣ API request | Περιορίζει πράξεις με βάση ημερομηνία καταχώρισης στο ΚΗΜΔΗΣ. |
| `cpvItems` | ΚΗΜΔΗΣ API request | Περιορίζει με βάση τους CPV κωδικούς που στέλνονται. |
| `organizations` | ΚΗΜΔΗΣ API request, μόνο όταν δίνεται | Περιορίζει με βάση κωδικό φορέα ΚΗΜΔΗΣ. |
| `referenceNumber` | ΚΗΜΔΗΣ API request | Αναζητά συγκεκριμένο ΑΔΑΜ. |
| `totalCostFrom` / `totalCostTo` | ΚΗΜΔΗΣ API request, μόνο στη Γενική Αναζήτηση | Περιορίζει βάσει ποσού όταν υποστηρίζεται από το resource. |
| `finalDateFrom` / `finalDateTo` | ΚΗΜΔΗΣ API request, μόνο σε `notice` και μόνο στη Γενική Αναζήτηση | Περιορίζει βάσει καταληκτικής ημερομηνίας προσφορών. |

Το ΚΗΜΔΗΣ εφαρμόζει κανόνα 180 ημερών σε ημερομηνιακά πεδία: όταν λείπει ένα άκρο του εύρους ή όταν δεν δοθεί εύρος, το API ορίζει αυτόματα παράθυρο έως 180 ημέρες. Αν δοθεί εύρος μεγαλύτερο των 180 ημερών, περιορίζεται σύμφωνα με τους κανόνες του API.

### 6.2 Local περιορισμοί

| Περιορισμός | Πού εφαρμόζεται | Επίδραση |
|---|---|---|
| `KHMDHS_MAX_PAGES` | Client pagination loop | Σταματά την ανάκτηση μετά από συγκεκριμένο αριθμό σελίδων. |
| Database pagination | Dashboard | Μετρά, φιλτράρει και ανακτά από PostgreSQL μόνο τα 20 αποτελέσματα της τρέχουσας σελίδας, διατηρώντας όλα τα ενεργά φίλτρα. |
| Περιοχές NUTS | Scoring / UI filters | Δεν μειώνει το API response· διαχωρίζεται σε τόπο εκτέλεσης (`nutsCodes`) και έδρα φορέα (`nutsCode`). |
| Budget προφίλ | Scoring | Δεν αποστέλλεται στο παραγωγικό ingest· χρησιμοποιείται στη βαθμολόγηση. |
| Deadline status/date range | Dashboard/reports filters | Δεν περιορίζει το παραγωγικό ingest· περιορίζει την προβολή με κατάσταση ή ακριβές εύρος καταληκτικής ημερομηνίας. |
| Exports | Reports | Περιλαμβάνουν όλα τα αποτελέσματα των ενεργών φίλτρων χωρίς σιωπηρό όριο 1.000 γραμμών· η προεπισκόπηση οθόνης παραμένει στις πρώτες 100. |

Τα ενεργά προφίλ απαιτούν τουλάχιστον έναν έγκυρο CPV του καταλόγου. Τα budget πρέπει να είναι μη αρνητικοί αριθμοί και το ελάχιστο όριο δεν μπορεί να υπερβαίνει το μέγιστο.

Με `KHMDHS_MAX_PAGES=20`, κάθε εκτέλεση μπορεί να ανακτήσει έως 20 νέες σελίδες ανά query. Αν υπάρχουν περισσότερες, η εφαρμογή κρατά durable checkpoint και η επόμενη εκτέλεση συνεχίζει από την επόμενη σελίδα αντί να ξεκινήσει από το μηδέν.

---

## 7. Rate limiting και safe handling ΚΗΜΔΗΣ

Το επίσημο OpenData API του ΚΗΜΔΗΣ έχει όριο 350 αιτημάτων ανά λεπτό και τα δεδομένα του ανανεώνονται μία φορά ανά 24 ώρες. Η εφαρμογή χρησιμοποιεί proactive pacing, cache ίδιων queries και durable checkpoints, μαζί με προσαρμοστικό slowdown σε HTTP `429 Too Many Requests` και retries για προσωρινά read/connect errors.

Ρυθμίσεις:

| Μεταβλητή | Προεπιλογή | Περιγραφή |
|---|---:|---|
| `KHMDHS_REQUESTS_PER_MINUTE` | `180` | Στόχος pacing, με εσωτερικό safety cap 300/min. |
| `KHMDHS_CPV_BATCH_SIZE` | `100` | Μέγιστοι CPV ανά query. Κάθε batch έχει ανεξάρτητο checkpoint. |
| `KHMDHS_QUERY_CACHE_HOURS` | `20` | Διάρκεια επαναχρησιμοποίησης ενός ολοκληρωμένου ίδιου query. |
| `KHMDHS_SYNC_OVERLAP_DAYS` | `1` | Επικάλυψη ημερών στο incremental sync για καθυστερημένες εγγραφές. |
| `KHMDHS_CONTINUATION_DELAY_SECONDS` | `15` | Cooldown πριν από το επόμενο αυτόματο pagination chunk. |
| `KHMDHS_CONTINUATION_MAX_ATTEMPTS` | `50` | Πλήθος γρήγορων συνεχίσεων πριν από μεγαλύτερο cooldown. Η αλυσίδα δεν εγκαταλείπεται. |
| `KHMDHS_CONTINUATION_COOLDOWN_SECONDS` | `900` | Μεγαλύτερη αναμονή μετά από 50 γρήγορες συνέχειες· το ingest δεν εγκαταλείπεται. |
| `KHMDHS_RATE_LIMIT_RETRIES` | `4` | Πλήθος επαναλήψεων μετά από 429. |
| `KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS` | `5.0` | Βασική καθυστέρηση exponential backoff. |
| `KHMDHS_TRANSPORT_RETRIES` | `3` | Επαναλήψεις μετά από προσωρινό timeout/connection error. |
| `KHMDHS_TRANSPORT_BASE_DELAY_SECONDS` | `2.0` | Βάση exponential backoff για transport errors. |
| `KHMDHS_TIMEOUT_SECONDS` | `90` | Timeout ανά HTTP request. |
| `KHMDHS_INTERACTIVE_TIMEOUT_SECONDS` | `15` | Σύντομο timeout για αναζητήσεις ΚΗΜΔΗΣ από το UI. |
| `KHMDHS_INTERACTIVE_TRANSPORT_RETRIES` | `1` | Μία γρήγορη επανάληψη για προσωρινό σφάλμα στη χειροκίνητη αναζήτηση. |
| `INITIAL_PROFILE_INGEST_DAYS` | `30` | Εύρος της αυτόματης πρώτης εισαγωγής για νέο ενεργό προφίλ με CPV. |

Η συμπεριφορά είναι η εξής:

```text
Κανονική ροή:
  τελευταίο επιτυχημένο watermark → μικρό date overlap → νέα δεδομένα
  page N → durable checkpoint → page N+1 → ...
  όριο σελίδων/429/επίμονο timeout → delayed continuation job → συνέχεια από checkpoint
  ίδιο ολοκληρωμένο query εντός cache window → 0 API calls

Σε HTTP 429:
  αν υπάρχει Retry-After header, χρησιμοποιείται αυτό με ασφαλές cap
  αλλιώς εφαρμόζεται exponential backoff:
    5s, 10s, 20s, 40s ... ανάλογα με τις ρυθμίσεις
  μετά το όριο retries, το τρέχον search σταματά

Σε προσωρινό timeout/connection error:
  γίνονται έως 3 retries με exponential backoff
  αν το API εξακολουθεί να μην απαντά, το job δεν χάνει την πρόοδο
  δημιουργείται delayed continuation και συνεχίζει από το ίδιο page checkpoint
```

Ο limiter ξεκινά στον ρυθμό του `KHMDHS_REQUESTS_PER_MINUTE` και χρησιμοποιεί κοινό PostgreSQL reservation clock, ώστε worker και web process να μοιράζονται το ίδιο συνολικό pacing. Επιβραδύνει όταν λάβει 429. Τα CPV χωρίζονται σε σταθερά batches και κάθε συνδυασμός batch/τύπου ημερομηνίας έχει δικό του durable checkpoint. Το ημερήσιο scheduled ingest του `notice` είναι incremental και, αν ο worker ξεκινήσει μετά την προγραμματισμένη ώρα, γίνεται catch-up εφόσον δεν έχει ήδη ολοκληρωθεί το σημερινό global ingest.

Όταν ο client φτάσει σε rate limit ή επίμονο προσωρινό transport error μετά τα retries, αποθηκεύονται όσα αποτελέσματα είχαν ήδη ανακτηθεί και το watermark δεν προχωρά. Η αυτόματη συνέχεια ξεκινά από το αποθηκευμένο page checkpoint.

---

## 8. Εσωτερικά HTTP endpoints εφαρμογής

Όλα τα UI endpoints, εκτός από `/health`, προστατεύονται προαιρετικά με HTTP Basic Authentication όταν έχουν οριστεί `ADMIN_USERNAME` και `ADMIN_PASSWORD`.

| Method | Path | Τύπος | Περιγραφή |
|---|---|---|---|
| `GET` | `/health` | JSON | Health check. |
| `GET` | `/` | HTML | Dashboard ανά προφίλ, score, deadline/status filters, αναζήτηση και περιοχή. |
| `POST` | `/ingest/run` | Redirect | Χειροκίνητο ingest ΚΗΜΔΗΣ. Αν δοθεί profile, τρέχει για το επιλεγμένο προφίλ. |
| `POST` | `/rescore/run` | Redirect | Επανυπολογισμός σχετικότητας. Υποστηρίζει profile scope. |
| `GET` | `/kimdis` | HTML | Γενική Αναζήτηση ΚΗΜΔΗΣ σε live OpenData resources. |
| `POST` | `/kimdis/save` | Redirect | Profile-specific αποθήκευση/βαθμολόγηση αποτελέσματος Γενικής Αναζήτησης. Το score σημειώνεται ως `saved`. |
| `POST` | `/scores/{score_id}/workflow` | Redirect | Ενημέρωση workflow status και σημειώσεων για συγκεκριμένο score row. |
| `POST` | `/tenders/{tender_id}/delete` | Redirect | Οριστική διαγραφή διαγωνισμού από τη βάση. Διαγράφονται cascade οι αξιολογήσεις και οι σχετικές πράξεις Διαύγειας. |
| `GET` | `/tenders/{tender_id}` | HTML | Σελίδα λεπτομέρειας διαγωνισμού, scores, επίσημος σύνδεσμος ΚΗΜΔΗΣ, ΚΗΜΔΗΣ timeline, PDF και Διαύγεια enrichment. |
| `POST` | `/tenders/{tender_id}/diavgeia-refresh` | Redirect | Αναζήτηση και αποθήκευση σχετικών πράξεων Διαύγειας. |
| `POST` | `/tenders/{tender_id}/analyze-pdf` | Redirect | On-demand λήψη PDF, εξαγωγή κειμένου και επαναβαθμολόγηση. |
| `GET` | `/profiles` | HTML | Λίστα προφίλ. |
| `GET` | `/profiles/new` | HTML | Φόρμα δημιουργίας προφίλ. |
| `GET` | `/profiles/{profile_id}/edit` | HTML | Φόρμα επεξεργασίας προφίλ. |
| `POST` | `/profiles` | Redirect | Δημιουργία προφίλ. |
| `POST` | `/profiles/{profile_id}` | Redirect | Ενημέρωση προφίλ. |
| `POST` | `/profiles/{profile_id}/toggle` | Redirect | Ενεργοποίηση/απενεργοποίηση προφίλ. |
| `POST` | `/profiles/{profile_id}/delete` | Redirect | Διαγραφή προφίλ, εκτός αν είναι το τελευταίο. |
| `GET` | `/reports` | HTML | Σελίδα αναφορών ανά προφίλ με φίλτρα CPV match, workflow, προθεσμίας, ημερομηνιών, score και NUTS. |
| `GET` | `/reports/export` | File response | Εξαγωγή του ίδιου ακριβώς φιλτραρισμένου συνόλου σε PDF, CSV, JSONL ή Markdown. |
| `GET` | `/profiles/{profile_id}/export` | File response | Εξαγωγή προφίλ σε PDF ή Markdown. |
| `GET` | `/api/cpv/search` | JSON | Αναζήτηση CPV από τον τοπικό κατάλογο. |
| `GET` | `/api/cpv/children` | JSON | Ανάκτηση παιδιών CPV από τον τοπικό κατάλογο. |
| `GET` | `/maintenance` | HTML | Τεχνική σελίδα συντήρησης και usage summary. |
| `GET` | `/activity` | HTML | System event log. |
| `GET` | `/api/tenders` | JSON | Περιορισμένο JSON endpoint για scores άνω του `min_score`. |

---

## 9. Μοντέλο δεδομένων

### 9.1 `client_profiles`

Αποθηκεύει προφίλ ενδιαφέροντος. Περιλαμβάνει CPV, prefixes, preferred regions, budget range, required certificates και ενεργή/ανενεργή κατάσταση. Οι παλιές στήλες keywords/RSS παραμένουν μόνο για συμβατότητα βάσης και δεν συμμετέχουν στη λειτουργία.

### 9.2 `tenders`

Κοινή αποθήκη πράξεων. Το μοναδικότητα ορίζεται από `source + source_reference`. Περιλαμβάνει ΑΔΑΜ, τίτλο, φορέα, ημερομηνίες, ποσά, CPV, official URL, attachment URL, raw JSON, PDF text και ingest markers.

### 9.3 `tender_scores`

Πίνακας συσχέτισης διαγωνισμού με προφίλ. Περιλαμβάνει `score`, `rule_score`, matched CPV, reasons, recommended action, workflow status, user notes και profile-specific latest ingest markers. Υπάρχει unique constraint `tender_id + profile_id`.

### 9.4 `diavgeia_decisions`

Σχετικές πράξεις Διαύγειας ανά tender. Περιλαμβάνει ΑΔΑ, subject, organization/decision type IDs, ημερομηνίες, status, public URL, API URL και raw JSON. Υπάρχει unique constraint `tender_id + ada` για deduplication.

Από v0.10.1, η σελίδα λεπτομέρειας διαβάζει structured fields από `raw.extraFieldValues`, όπως:

- CPV Διαύγειας,
- εκτιμώμενο ποσό,
- σχετικό ΑΔΑ,
- related decisions,
- protocol number,
- PDF document URL.

### 9.5 `system_events`

Καταγράφει ingest, warnings, profile changes, rescore, PDF analysis, Διαύγεια refresh και άλλα τεχνικά γεγονότα.

---

## 10. Scoring

Το score υπολογίζεται ανά `tender + profile`. Δεν υπάρχει ενιαίο global score για έναν διαγωνισμό. Ο ίδιος ΑΔΑΜ μπορεί να έχει διαφορετικό score, διαφορετικό workflow status και διαφορετική ένδειξη νέου ευρήματος ανά προφίλ.

Ένα αυτόματο `tender_score` αποθηκεύεται μόνο όταν υπάρχει matched CPV με το προφίλ. Budget και περιοχή χρησιμοποιούνται για την κατάταξη ενός σχετικού αποτελέσματος, αλλά δεν αρκούν από μόνα τους για να συνδέσουν ολόκληρη την κοινή βάση tenders με κάθε προφίλ. Επομένως, φίλτρο ελάχιστου score `0` εμφανίζει τα χαμηλά αποτελέσματα του επιλεγμένου προφίλ και όχι άσχετα tenders άλλων προφίλ. Εξαίρεση αποτελούν όσα αποθηκεύτηκαν ή σχολιάστηκαν ρητά από τον χρήστη, τα οποία διατηρούνται ως ιστορική πρόθεση.

Ενδεικτική ερμηνεία:

| Score | Ερμηνεία | Recommended action |
|---:|---|---|
| `0–54` | Χαμηλή σχετικότητα. | `ignore` |
| `55–74` | Πιθανή ευκαιρία / μεσαία προτεραιότητα. | `review` |
| `75–100` | Υψηλή προτεραιότητα. | `bid` |

### 10.1 Μοντέλο rule-based βαθμολόγησης

Το CPV καθορίζει πρώτα μία σαφή κατηγορία και τη βασική της βαθμολογία. Τα πρόσθετα κριτήρια του προφίλ δεν μεταφέρουν ποτέ ένα αποτέλεσμα σε άλλη CPV κατηγορία· απλώς το κατατάσσουν λίγο χαμηλότερα μέσα στη δική του κατηγορία όταν υπάρχει πραγματική ασυμφωνία.

| Κατηγορία CPV | Βασικό score | Κατώτατο score κατηγορίας |
|---|---:|---:|
| Ακριβές match: όλα τα CPV του διαγωνισμού είναι επιλεγμένα | `100` | `86` |
| Μερικό match: υπάρχει τουλάχιστον ένα ακριβές CPV και επιπλέον μη ακριβή CPV | `85` | `56` |
| Child/broad match: μόνο παιδί, απόγονος ή prefix επιλεγμένου CPV | `55` | `35` |
| Κανένα CPV match | `0` | `0` |

Έτσι ακόμη και ένα ακριβές match που αστοχεί σε όλα τα πρόσθετα κριτήρια παραμένει πάνω από το μέγιστο του μερικού match, και αντίστοιχα το μερικό παραμένει πάνω από το broad. Η προθεσμία και η ακύρωση δεν αλλάζουν τη σχετικότητα.

### 10.2 Πρόσθετα κριτήρια

Τα κριτήρια budget, περιοχής και απαιτήσεων λειτουργούν ως μικρά penalties μόνο όταν το προφίλ τα έχει ενεργοποιήσει και υπάρχουν αρκετά δεδομένα για ασφαλές συμπέρασμα. Match ή έλλειψη δεδομένων είναι ουδέτερα: δεν προστίθενται bonus βαθμοί και δεν επιβάλλεται ποινή.

### 10.3 CPV scoring

Το CPV είναι το μοναδικό κριτήριο που αποφασίζει σε ποιο section ανήκει το αποτέλεσμα. Αν ο διαγωνισμός έχει μόνο CPV που έχουν επιλεγεί ακριβώς στο προφίλ, είναι «Ακριβές match». Αν έχει τουλάχιστον ένα ακριβές και επιπλέον CPV, είναι «Μερικό match», ανεξάρτητα αν το ακριβές είναι `1/2` ή `1/16`. Αν δεν υπάρχει ακριβές αλλά υπάρχει παιδί/απόγονος ή prefix, είναι «Child CPV».

Όλα τα πραγματικά CPV matches διατηρούνται στη βάση και εμφανίζονται στο αντίστοιχο section. Τα default φίλτρα dashboard, reports και JSON API ξεκινούν από score `0`, ώστε ένα broad match που μειώθηκε από πρόσθετα κριτήρια να μη χάνεται.

### 10.4 Budget scoring

Το budget αξιολογείται μόνο όταν ο διαγωνισμός παρέχει χρησιμοποιήσιμο ποσό χωρίς ΦΠΑ.

| Περίπτωση | Επίδραση |
|---|---:|
| Ποσό εντός min/max ορίων προφίλ | ουδέτερο |
| Ποσό κάτω από το ελάχιστο | `-5` |
| Ποσό πάνω από το μέγιστο | `-5` |
| Μη διαθέσιμο ποσό | ουδέτερο |

Το μη διαθέσιμο ποσό θεωρείται έλλειψη δεδομένων και όχι αρνητική ένδειξη.

### 10.5 Region / NUTS scoring

Οι περιοχές δεν αποστέλλονται ως φίλτρο στο ΚΗΜΔΗΣ OpenData search. Αξιολογούνται τοπικά μετά την ανάκτηση. Το `nutsCodes` αποτελεί το ισχυρό structured σήμα τόπου εκτέλεσης. Μόνο όταν λείπει χρησιμοποιείται το `nutsCode` της Αναθέτουσας Αρχής ως ασθενέστερο fallback.

| Περίπτωση | Επίδραση |
|---|---:|
| Match στον τόπο εκτέλεσης (`nutsCodes`) | ουδέτερο |
| Match στην έδρα φορέα όταν λείπει τόπος εκτέλεσης | `-2` |
| Υπάρχει γεωγραφικό σήμα αλλά δεν ταιριάζει με το προφίλ | `-4` |
| Δεν υπάρχουν επαρκή γεωγραφικά στοιχεία | ουδέτερο |

Η διάκριση strong/weak match έχει σκοπό να μειώσει false positives, π.χ. περιπτώσεις όπου ένας όρος περιοχής εμφανίζεται σε κείμενο χωρίς να δηλώνει πραγματικό τόπο εκτέλεσης.

### 10.6 Απαιτήσεις, πιστοποιητικά και PDF text

Οι απαιτήσεις/πιστοποιητικά αξιολογούνται κυρίως όταν υπάρχει αναλυμένο κείμενο PDF ή άλλο επαρκές διαθέσιμο κείμενο.

| Περίπτωση | Επίδραση |
|---|---:|
| Εντοπίζονται όλες οι απαιτήσεις | ουδέτερο |
| Υπάρχει PDF/text και λείπει μία ή περισσότερες απαιτήσεις | `-5` συνολικά |
| Δεν έχει γίνει PDF analysis | ουδέτερο |

Η ουδέτερη συμπεριφορά πριν από το PDF analysis αποτρέπει ψευδείς αρνητικές βαθμολογίες όταν τα σχετικά κριτήρια βρίσκονται μόνο μέσα στη διακήρυξη.

### 10.7 Deadline και cancellation ως κατάσταση

Η λήξη και η ακύρωση δεν προσθέτουν ούτε αφαιρούν βαθμούς. Το relevance score παραμένει σταθερό και η πράξη χαρακτηρίζεται ανεξάρτητα ως ενεργή, άγνωστης προθεσμίας, ληγμένη ή ακυρωμένη/ματαιωμένη. Τα default dashboard, reports, API αποτελέσματα, ingest counters και notifications περιλαμβάνουν μόνο ενεργές ή άγνωστης προθεσμίας μη ακυρωμένες πράξεις. Τα ληγμένα και ακυρωμένα παραμένουν διαθέσιμα στο ιστορικό και στα ρητά φίλτρα.

### 10.9 Τελικό score

Το τελικό score είναι το rule-based score του προφίλ. Αν γίνει ανάλυση PDF και αποθηκευτεί extracted text, το ίδιο rule-based scoring μπορεί να ξανατρέξει με περισσότερα διαθέσιμα κείμενα, χωρίς χρήση εξωτερικού μοντέλου.

### 10.10 Ενδεικτικές συνέπειες της λογικής

- Exact CPV match σε ειδικό leaf code μπορεί να οδηγήσει σε υψηλό relevance score ανεξάρτητα από την κατάσταση προθεσμίας.
- Exact match σε πολύ γενικό parent CPV αποδίδει χαμηλότερη εμπιστοσύνη, επειδή δηλώνει ευρεία κατηγορία και όχι απαραίτητα συγκεκριμένη συνάφεια.
- Descendant match από γενικό parent CPV θεωρείται χρήσιμο για discovery, αλλά συνήθως οδηγεί σε `review` και όχι αυτόματα σε `bid`.
- Περιοχές, budget και απαιτήσεις δεν τιμωρούν όταν λείπουν τα αναγκαία δεδομένα από το ΚΗΜΔΗΣ ή δεν έχει γίνει PDF analysis.
- Η Διαύγεια δεν συμμετέχει στη βαθμολόγηση στην τρέχουσα έκδοση. Χρησιμοποιείται ως evidence/context panel.

---

## 11. Workflow status και ένδειξη νέου ευρήματος

Η εφαρμογή διαχωρίζει δύο έννοιες.

### Workflow status

Χειροκίνητη κατάσταση εργασίας ανά `tender_score`:

- `new` — χωρίς ενέργεια,
- `saved` — αποθηκευμένο,
- `reviewing` — σε έλεγχο,
- `not_relevant` — δεν αφορά.

Το status είναι profile-specific. Ένας διαγωνισμός μπορεί να είναι αποθηκευμένος για ένα προφίλ και ουδέτερος ή μη σχετικός για άλλο. Το παραγωγικό ingest δημιουργεί νέα score rows με ουδέτερη κατάσταση `new`. Αντίθετα, η χειροκίνητη αποθήκευση από τη Γενική Αναζήτηση ΚΗΜΔΗΣ θεωρείται ρητή ενέργεια επιλογής από τον χρήστη και, από v0.10.5, θέτει αυτόματα το score του επιλεγμένου προφίλ σε `saved`.

### New from latest ingest

Τεχνική ένδειξη ingest. Δηλώνει ότι το συγκεκριμένο tender έγινε ορατό για το συγκεκριμένο προφίλ στην τελευταία εισαγωγή. Ένας παλιός ΑΔΑΜ μπορεί να είναι νέος για ένα νέο ή διαφορετικό προφίλ.

### Οριστική διαγραφή tender

Η οριστική διαγραφή εκτελείται μέσω `POST /tenders/{tender_id}/delete`. Η ενέργεια διαγράφει την εγγραφή από τον πίνακα `tenders` και, λόγω cascade σχέσεων, διαγράφει επίσης τα αντίστοιχα `tender_scores` και `diavgeia_decisions`. Καταγράφεται system event τύπου `tender_deleted`.

Η διαγραφή δεν λειτουργεί ως μόνιμη εξαίρεση/blacklist έναντι του ΚΗΜΔΗΣ. Αν ο ίδιος ΑΔΑΜ εξακολουθεί να ταιριάζει σε μελλοντικό ingest ή αποθηκευτεί ξανά από τη Γενική Αναζήτηση, μπορεί να δημιουργηθεί νέα εγγραφή. Για απλή απόκρυψη από τη ροή εργασίας προτιμάται το workflow status `not_relevant`.

---

## 12. Έλεγχος απαιτήσεων PDF

Ο έλεγχος ξεκινά από το `/tenders/{tender_id}/analyze-pdf` και εκτελείται ως background job από τον worker, ώστε το web request να επιστρέφει αμέσως. Το UI κρατά μία σύνδεση Server-Sent Events (SSE) για την κατάσταση της συγκεκριμένης εργασίας, εμφανίζει ένδειξη προόδου και ανανεώνει τη σελίδα όταν ολοκληρωθεί. Το σύστημα ανακτά το official attachment από το ΚΗΜΔΗΣ και δοκιμάζει πρώτα τη γρήγορη εξαγωγή ενσωματωμένου κειμένου.

Αν το PDF δεν έχει επαρκές text layer, ενεργοποιείται αυτόματα bounded OCR με Tesseract (`ell+eng`). Το OCR εφαρμόζεται μόνο τότε, έως το ρυθμιζόμενο όριο σελίδων, ώστε τα κανονικά PDF να παραμένουν γρήγορα και ένα μεγάλο scan να μη δεσμεύει απεριόριστα τον worker.

- Το καθημερινό ingest δεν κατεβάζει μαζικά PDFs, εκτός αν ενεργοποιηθεί ρητά `AUTO_FETCH_PDF_TEXT=true`.
- Μετά την εξαγωγή, ο διαγωνισμός επαναβαθμολογείται για τα σχετικά προφίλ.
- Αν ούτε το embedded text ούτε το OCR αποδώσουν κείμενο, το UI εμφανίζει σαφές μήνυμα αντί για γενικό runtime error.

Ρυθμίσεις OCR: `PDF_OCR_ENABLED`, `PDF_OCR_MAX_PAGES`, `PDF_OCR_DPI`, `PDF_OCR_LANGUAGES` και `PDF_OCR_PAGE_TIMEOUT_SECONDS`.

---

## 13. Διαύγεια enrichment

Η λειτουργία Διαύγειας εκτελείται από το `/tenders/{tender_id}/diavgeia-refresh`. Η αναζήτηση βασίζεται στον διαθέσιμο ΑΔΑΜ (`reference_number` ή fallback `source_reference`) και επιστρέφει σχετικά Διαύγεια decisions. Η αναζήτηση είναι συντηρητική: το σύστημα αποθηκεύει μόνο αποτελέσματα που επιστρέφονται από αναζήτηση με τον κωδικό της πράξης και δεν αποθηκεύει αυτόματα υποψήφια αποτελέσματα που θα μπορούσαν να προκύψουν από ελεύθερο τίτλο, CPV ή φορέα.

Ροή:

```text
Tender ΚΗΜΔΗΣ
  → ΑΔΑΜ
  → Διαύγεια search
  → optional hydration ανά ΑΔΑ
  → αποθήκευση σε diavgeia_decisions
  → εμφάνιση στη σελίδα λεπτομέρειας
```

Η αποθήκευση έχει deduplication ανά `tender_id + ada`. Η τρέχουσα υλοποίηση αποθηκεύει IDs όπως `organizationId` και `decisionTypeId` όταν το API δεν επιστρέφει readable labels. Τα readable lookup dictionaries αποτελούν επόμενο πιθανό patch.

Το enrichment δεν αλλάζει το score. Παρέχει evidence panel με πρόσθετα στοιχεία όπως ΑΔΑ, status, ημερομηνία, CPV Διαύγειας, ποσό, σχετικός ΑΔΑ, αριθμός πρωτοκόλλου και PDF link. Αν δεν βρεθεί αποτέλεσμα, το UI αναφέρει ότι δεν εντοπίστηκε ασφαλές exact match με ΑΔΑΜ/κωδικό, χωρίς να αποκλείει την ύπαρξη διοικητικού ιστορικού εκτός του συγκεκριμένου search term.


### 13.1 Επίσημος σύνδεσμος ΚΗΜΔΗΣ στη λεπτομέρεια

Η σελίδα `/tenders/{tender_id}` εμφανίζει επίσημο σύνδεσμο προς το περιβάλλον ΚΗΜΔΗΣ όταν υπάρχει `tender.url` ή διαθέσιμο `reference_number`. Ο σύνδεσμος κατασκευάζεται ως αναζήτηση βάσει ΑΔΑΜ:

```text
https://cerpp.eprocurement.gov.gr/khmdhs/search?referenceNumber={referenceNumber}
```

Ο σύνδεσμος παρέχεται ως operational convenience. Η πρόσβαση στο επίσημο περιβάλλον ΚΗΜΔΗΣ μπορεί να απαιτεί credentials ή δικαιώματα χρήστη και δεν αποτελεί προϋπόθεση για τη λειτουργία της τοπικής εφαρμογής.

Όταν το raw response περιέχει έγκυρο `biddingWebsite`, η λεπτομέρεια εμφανίζει επιπλέον «Πλατφόρμα υποβολής» και τους διαθέσιμους `systemicNumbers`. Το πεδίο είναι προαιρετικό και συχνά οδηγεί στη γενική αρχική σελίδα του ΕΣΗΔΗΣ, συνεπώς ο συστημικός αριθμός παραμένει απαραίτητος για την ακριβή αναζήτηση.

---

## 14. Reports

Οι αναφορές βασίζονται στον πίνακα `tender_scores` και είναι profile-oriented. Υποστηρίζουν φίλτρα προφίλ, περιόδου, score, ενεργής/ληγμένης προθεσμίας, αναζήτησης, τόπου εκτέλεσης και έδρας Αναθέτουσας Αρχής.

Scopes:

| Scope | Περιγραφή |
|---|---|
| `matches` | Πιθανές ευκαιρίες με βάση threshold και φίλτρα. |
| `latest_new` | Νέα ευρήματα τελευταίου ingest για το επιλεγμένο προφίλ. |
| `saved_reviewing` | Χειροκίνητη shortlist. |
| `not_rejected` | Όλα τα μη απορριφθέντα. |

Formats:

- PDF,
- CSV,
- JSONL,
- Markdown,
- Markdown (`format=md`).

Οι αναφορές περιλαμβάνουν πλέον το πλαίσιο του επιλεγμένου προφίλ: όνομα, αποθηκευμένη περιγραφή επιχείρησης/δυνατοτήτων, CPV, prefixes, απαιτήσεις, NUTS και εύρος προϋπολογισμού.

Για τα PDF διακηρύξεων η προτεινόμενη πρακτική είναι:

- Η απλή Markdown/PDF αναφορά περιλαμβάνει το URL του επίσημου PDF και ένδειξη αν υπάρχει extracted PDF text στη βάση.
- Η Markdown εξαγωγή μπορεί να περιλάβει σύντομο απόσπασμα extracted PDF text όπου υπάρχει, ώστε να βοηθά τον γρήγορο προέλεγχο.
- Για πλήρη έλεγχο διακήρυξης, ανοίξτε και το raw/official PDF, επειδή το extracted text μπορεί να είναι ελλιπές ή να μην υπάρχει σε scanned PDFs.

---

## 15. Ρυθμίσεις περιβάλλοντος

| Μεταβλητή | Ρόλος |
|---|---|
| `DATABASE_URL` | SQLAlchemy connection string. Στο Docker Compose δείχνει στο service `postgres`. |
| `KHMDHS_BASE_URL` | Base URL ΚΗΜΔΗΣ. |
| `KHMDHS_TIMEOUT_SECONDS` | Timeout ανά ΚΗΜΔΗΣ request. |
| `KHMDHS_MAX_PAGES` | Μέγιστες σελίδες ανά paginated ΚΗΜΔΗΣ search στο παραγωγικό client. |
| `KHMDHS_REQUESTS_PER_MINUTE` | Proactive pacing των paginated requests. |
| `KHMDHS_CPV_BATCH_SIZE` | Μέγιστο πλήθος CPV ανά ανεξάρτητο API query/checkpoint. |
| `KHMDHS_QUERY_CACHE_HOURS` | TTL cache ολοκληρωμένων ίδιων queries. |
| `KHMDHS_SYNC_OVERLAP_DAYS` | Επικάλυψη ημερών στο incremental sync. |
| `KHMDHS_CONTINUATION_DELAY_SECONDS` | Αναμονή πριν από self-continuation job. |
| `KHMDHS_CONTINUATION_MAX_ATTEMPTS` | Όριο γρήγορων συνεχίσεων πριν ενεργοποιηθεί cooldown. |
| `KHMDHS_CONTINUATION_COOLDOWN_SECONDS` | Cooldown πριν συνεχίσει ένας μακρύς ingest κύκλος. |
| `KHMDHS_RATE_LIMIT_RETRIES` | Retries μετά από HTTP 429. |
| `KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS` | Βάση exponential backoff μετά από 429. |
| `KHMDHS_TRANSPORT_RETRIES` | Retries μετά από προσωρινό read/connect error. |
| `KHMDHS_TRANSPORT_BASE_DELAY_SECONDS` | Βάση exponential backoff για transport errors. |
| `DIAVGEIA_BASE_URL` | Base URL Διαύγειας για OpenData calls. |
| `DIAVGEIA_TIMEOUT_SECONDS` | Timeout ανά Διαύγεια request. |
| `DIAVGEIA_DEFAULT_PAGE_SIZE` | Default μέγεθος σελίδας σε Διαύγεια searches. |
| `SCHEDULE_HOUR` / `SCHEDULE_MINUTE` | Ώρα ημερήσιου scheduler. |
| `INGEST_DAYS_BACK` | Παράθυρο ημερών για ΚΗΜΔΗΣ ingest. |
| `MATCH_THRESHOLD` | Default όριο σχετικότητας για dashboard/reports. |
| `FETCH_PDF_FOR_SCORE_ABOVE` | Threshold για προαιρετική αυτόματη λήψη PDF text. |
| `AUTO_FETCH_PDF_TEXT` | Αν είναι true, επιτρέπει μαζικό PDF fetch στο ingest υπό προϋποθέσεις. Default false. |
| `PDF_OCR_ENABLED` | Ενεργοποιεί OCR fallback μόνο για PDF χωρίς επαρκές text layer. Default true. |
| `PDF_OCR_MAX_PAGES` | Μέγιστες σελίδες OCR ανά PDF. Default 10. |
| `PDF_OCR_DPI` | Ανάλυση rendering πριν το OCR. Default 200. |
| `PDF_OCR_LANGUAGES` | Γλώσσες Tesseract. Default `ell+eng`. |
| `PDF_OCR_PAGE_TIMEOUT_SECONDS` | Timeout Tesseract ανά σελίδα. Default 30. |
| `INITIAL_PROFILE_INGEST_DAYS` | Ημέρες αυτόματης πρώτης εισαγωγής νέου προφίλ. Default 30. |
| `APP_TIMEZONE` | Ζώνη ώρας εμφάνισης και reports. |
| `APP_ENV` | Runtime mode. Set `production` on customer/server deployments. |
| `SESSION_SECRET_KEY` | Required in production. Signs browser sessions; use a long random value. |
| `SESSION_COOKIE_SECURE` | Set `true` when the app is served over HTTPS. |
| `REQUIRE_SESSION_SECRET` | Forces failure when `SESSION_SECRET_KEY` is missing. |
| `CSRF_PROTECTION_ENABLED` | Enables CSRF validation for browser POST forms. |
| `LOGIN_RATE_LIMIT_ATTEMPTS` / `LOGIN_RATE_LIMIT_WINDOW_SECONDS` | In-memory login throttling for repeated failures. |
| `MIN_PASSWORD_LENGTH` | Minimum password length for bootstrap/admin-created users. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Legacy Basic Auth fallback. Prefer user login plus `BOOTSTRAP_ADMIN_*`. |
| `SMTP_*`, `DIGEST_RECIPIENTS` | Προαιρετικό email digest. |

Παράδειγμα παραγωγικής ρύθμισης ΚΗΜΔΗΣ με ήπιο request pacing:

```env
KHMDHS_TIMEOUT_SECONDS=90
KHMDHS_MAX_PAGES=20
KHMDHS_REQUESTS_PER_MINUTE=180
KHMDHS_CPV_BATCH_SIZE=100
KHMDHS_QUERY_CACHE_HOURS=20
KHMDHS_SYNC_OVERLAP_DAYS=1
KHMDHS_CONTINUATION_DELAY_SECONDS=15
KHMDHS_CONTINUATION_MAX_ATTEMPTS=50
KHMDHS_CONTINUATION_COOLDOWN_SECONDS=900
KHMDHS_RATE_LIMIT_RETRIES=4
KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS=5.0
KHMDHS_TRANSPORT_RETRIES=3
KHMDHS_TRANSPORT_BASE_DELAY_SECONDS=2.0
INGEST_DAYS_BACK=3
```

---

## 16. Deployment και runtime components

Το Docker Compose περιλαμβάνει:

| Service | Ρόλος |
|---|---|
| `web` | FastAPI web application. |
| `worker` | Scheduler για ημερήσιο ingest. |
| `postgres` | PostgreSQL database. |

Η βάση αρχικοποιείται από την εφαρμογή μέσω SQLAlchemy metadata. Δεν υπάρχει ξεχωριστό migration framework στην τρέχουσα έκδοση.

Ενδεικτικές τεχνικές εντολές λειτουργίας:

```powershell
docker compose up --build -d
docker compose logs --tail=150 web
docker compose logs --tail=150 worker
docker compose run --rm web pytest -q
```

Οι εντολές αυτές αποτελούν operational reference και όχι απαίτηση χρήσης από τελικό χρήστη.

---

## 17. Ασφάλεια και διαχείριση μυστικών

Το `.env` δεν πρέπει να αποθηκεύεται σε Git repository. Περιλαμβάνει δυνητικά κλειδιά API, credentials web interface και SMTP credentials.

Για local/demo χρήση το `APP_ENV=development` μπορεί να χρησιμοποιεί development session fallback. Για χρήση από πελάτη ή server deployment πρέπει να οριστούν `APP_ENV=production`, μακρύ τυχαίο `SESSION_SECRET_KEY`, `BOOTSTRAP_ADMIN_*` για τον πρώτο admin χρήστη και `SESSION_COOKIE_SECURE=true` όταν η εφαρμογή σερβίρεται μέσω HTTPS. Τα `ADMIN_USERNAME` / `ADMIN_PASSWORD` παραμένουν μόνο ως legacy Basic Auth fallback.

Η εφαρμογή λειτουργεί με rule-based scoring και δεν απαιτεί εξωτερικό μοντέλο.

---

## 18. Γνωστοί περιορισμοί

| Περιορισμός | Επίδραση |
|---|---|
| Δεν υπάρχει upstream NUTS filter στο τεκμηριωμένο ΚΗΜΔΗΣ `notice` search | Οι περιοχές εφαρμόζονται μετά την ανάκτηση, όχι στο API request. |
| Το upstream API μπορεί προσωρινά να καθυστερεί | Γίνονται transport retries και, αν εξαντληθούν, self-continuation από durable checkpoint. |
| Περιορισμοί OCR | Χειρόγραφα, χαμηλή ανάλυση και σύνθετες σαρώσεις μπορεί να μην αποδώσουν αξιόπιστο κείμενο. |
| Δεν γίνεται μαζικό PDF download default | Το scoring πριν το PDF analysis βασίζεται σε metadata. |
| Πολύ γενικά parent CPV μπορούν να επιστρέψουν μεγάλο όγκο | Επηρεάζονται από `KHMDHS_MAX_PAGES`, date window και rate limits. |
| Διαύγεια labels δεν είναι πλήρως resolved | Αποθηκεύονται IDs όταν το API δεν επιστρέφει readable names. |
| Το Διαύγεια enrichment δεν επηρεάζει score | Παρέχει evidence/context, όχι decision automation. |

---

## 19. Πιθανά επόμενα τεχνικά βήματα

| Πεδίο | Περιγραφή |
|---|---|
| Proactive ΚΗΜΔΗΣ limiter | Προσθήκη `KHMDHS_REQUESTS_PER_MINUTE` και shared request window counter. |
| Διαύγεια dictionaries | Lookup/cache για organization names, decision type labels, units και signers. |
| Migration framework | Εισαγωγή Alembic για ελεγχόμενες αλλαγές schema. |

---

## 20. Changelog

### v0.10.6

- Διευκρινίστηκε στο προϊόν και στο README ότι η Διαύγεια λειτουργεί ως secondary evidence layer.
- Η σελίδα λεπτομέρειας διαγωνισμού εμφανίζει πλέον επίσημο σύνδεσμο “Άνοιγμα στο ΚΗΜΔΗΣ”, με σημείωση ότι ενδέχεται να απαιτούνται credentials.
- Το Διαύγεια panel αναδιατυπώθηκε ώστε να εξηγεί τον συντηρητικό ΑΔΑΜ-based έλεγχο και να αποφεύγει aggressive fallback auto-save από τίτλο/CPV/φορέα.
- Τα μηνύματα μη εύρεσης Διαύγειας αποσαφηνίζουν ότι δεν βρέθηκε ασφαλές exact match, όχι ότι δεν υπάρχει διοικητικό ιστορικό.
- Έγινε συνολικό UI refinement σε navigation, cards, action bars, metadata grids, empty states και evidence cards.

### v0.10.5

- Η χειροκίνητη αποθήκευση από τη Γενική Αναζήτηση ΚΗΜΔΗΣ θέτει αυτόματα το score του επιλεγμένου προφίλ σε `saved`.
- Προστέθηκε endpoint οριστικής διαγραφής διαγωνισμού από τη βάση: `POST /tenders/{tender_id}/delete`.
- Προστέθηκε διακριτικό κουμπί διαγραφής σε dashboard, detail page και Γενική Αναζήτηση ΚΗΜΔΗΣ για ήδη αποθηκευμένες εγγραφές.
- Η διαγραφή tender διαγράφει cascade τα profile scores και τις σχετικές πράξεις Διαύγειας.

### v0.10.4

- Προστέθηκε αναλυτική τεκμηρίωση scoring με κριτήρια, βάρη, penalties, bonuses και adaptive denominator.
- Δεν αλλάζει application code ή database schema.

### v0.10.3

- Αναδιατύπωση README σε μορφή τεχνικής έκθεσης.
- Προσθήκη επίσημης περιγραφής ΚΗΜΔΗΣ/Διαύγειας integrations.
- Προσθήκη περιγραφής endpoints εφαρμογής.
- Προσθήκη τεκμηρίωσης Γενικής Αναζήτησης ΚΗΜΔΗΣ, παραγωγικού ingest, περιορισμών API και rate-limit handling.
- Προσθήκη ρητής τεκμηρίωσης για NUTS: local scoring/filtering, όχι upstream ΚΗΜΔΗΣ filter.
- Δεν αλλάζει application code ή database schema.

### v0.10.2

- Η Γενική Αναζήτηση ΚΗΜΔΗΣ απέκτησε επιλογή προφίλ αποθήκευσης/βαθμολόγησης.
- Το `/kimdis/save` απαιτεί `profile_id` και δημιουργεί/ενημερώνει score μόνο για το επιλεγμένο προφίλ.

### v0.10.1

- Προστέθηκε εμφάνιση structured Διαύγεια fields στη λεπτομέρεια διαγωνισμού από `raw.extraFieldValues`.

### v0.10.0

- Προστέθηκε read-only Διαύγεια enrichment στη σελίδα λεπτομέρειας διαγωνισμού.
