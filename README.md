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
| Προφίλ παρακολούθησης | Ορισμός CPV, λέξεων-κλειδιών, αρνητικών λέξεων, περιοχών NUTS, budget και απαιτήσεων. |
| Εισαγωγή ΚΗΜΔΗΣ | Ανάκτηση πράξεων από το ΚΗΜΔΗΣ OpenData API, κυρίως από τις Προσκλήσεις/Προκηρύξεις/Διακηρύξεις. |
| Γενική Αναζήτηση ΚΗΜΔΗΣ | Live αναζήτηση σε πολλαπλά ΚΗΜΔΗΣ resources για ad hoc έλεγχο και profile-specific αποθήκευση. |
| Scoring | Rule-based αξιολόγηση ανά προφίλ, με βάση CPV, keywords, απαιτήσεις, περιοχές, budget και προθεσμίες. |
| Dashboard | Επισκόπηση ευρημάτων ανά προφίλ, με φίλτρα προθεσμίας, status, score, αναζήτησης και περιοχής. |
| PDF analysis | On-demand λήψη και εξαγωγή ενσωματωμένου κειμένου από PDF ΚΗΜΔΗΣ. Δεν περιλαμβάνει OCR. |
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
| `auction` | `POST /khmdhs-opendata/auction?page=N` | Αναθέσεις. Χρήσιμες για έρευνα αγοράς. |
| `contract` | `POST /khmdhs-opendata/contract?page=N` | Συμβάσεις. Χρήσιμες για ιστορικό ποσών/αναδόχων. |
| `payment` | `POST /khmdhs-opendata/payment?page=N` | Πληρωμές. Χρήσιμες για ιστορικό δαπανών. |
| `adamChain` | `GET /khmdhs-opendata/adamChain/{referenceNumber}` | Συνδεδεμένες πράξεις ανά ΑΔΑΜ. Χρησιμοποιείται στη σελίδα λεπτομέρειας. |
| `attachment` | `GET /khmdhs-opendata/{resource}/attachment/{referenceNumber}` | Επίσημο PDF πράξης. Χρησιμοποιείται για on-demand PDF analysis. |

Το κύριο ingest χρησιμοποιεί μόνο το `notice`. Η Γενική Αναζήτηση ΚΗΜΔΗΣ μπορεί να χρησιμοποιήσει όλα τα παραπάνω paginated resources.

### 3.2 Διαύγεια OpenData API

**Επίσημο reference:** `https://diavgeia.gov.gr/api/help`  
**Προεπιλεγμένο base URL εφαρμογής:** `https://diavgeia.gov.gr/luminapi/opendata`

Η Διαύγεια χρησιμοποιείται ως **read-only enrichment layer**. Δεν αντικαθιστά το ΚΗΜΔΗΣ ως source ευκαιριών. Ο ρόλος της είναι η τεκμηρίωση σχετικών διοικητικών πράξεων γύρω από έναν διαγωνισμό, όπως προσκλήσεις, αποφάσεις, αναθέσεις, συμβάσεις, πληρωμές, σχετικοί ΑΔΑ και δομημένα πεδία οικονομικού/CPV ενδιαφέροντος.

Βασική θέση συστήματος:

```text
ΚΗΜΔΗΣ = primary source για ευκαιρίες και διαγωνισμούς
Διαύγεια = secondary evidence / enrichment για διοικητικό context
```

Στο τρέχον στάδιο η Διαύγεια δεν συμμετέχει στο score. Τα αποτελέσματά της εμφανίζονται στη λεπτομέρεια διαγωνισμού ως τεκμηρίωση. Η εφαρμογή δεν χρησιμοποιεί τη Διαύγεια ως δεύτερη κύρια μηχανή market intelligence, επειδή τα procurement-native ιστορικά δεδομένα αγοράς καλύπτονται ήδη από τα ΚΗΜΔΗΣ resources `request`, `auction`, `contract` και `payment` στη Γενική Αναζήτηση ΚΗΜΔΗΣ.

Η λειτουργία Διαύγειας ορίζεται με πέντε κανόνες προϊόντος:

1. Παραμένει panel τεκμηρίωσης στη λεπτομέρεια διαγωνισμού.
2. Εμφανίζει readable labels όπου αυτά επιστρέφονται από το API και διατηρεί IDs όταν δεν επιστρέφονται labels.
3. Όταν δεν υπάρχει ΑΔΑΜ match, εμφανίζει ρητό μήνυμα ότι δεν βρέθηκε ασφαλές exact match, όχι ότι δεν υπάρχει διοικητικό ιστορικό.
4. Δεν εκτελεί aggressive fallback auto-save με τίτλο/CPV/φορέα, ώστε να αποφεύγονται false positives.
5. Η τεκμηρίωση και το UI παρουσιάζουν τη Διαύγεια ως secondary evidence layer και το ΚΗΜΔΗΣ ως primary source ευκαιριών και market intelligence.

---

## 4. Χειρισμός NUTS και περιοχών

Το ΚΗΜΔΗΣ OpenData search για `notice` δεν παρέχει, στην τεκμηριωμένη request body μορφή, επίσημο φίλτρο τύπου `nutsCode` ή `nutsCodes` για περιορισμό των αποτελεσμάτων στο API request. Η τεκμηρίωση περιλαμβάνει πεδία όπως `title`, `cpvItems`, `organizations`, `signer`, `contractType`, `dateFrom`, `dateTo`, `totalCostFrom`, `totalCostTo`, `referenceNumber`, `procedureType`, `finalDateFrom`, `finalDateTo`, `aaht`, `publicFundingRefNum` και `isModified`, αλλά όχι NUTS search parameter.

Τα NUTS εμφανίζονται στα δεδομένα απάντησης ή στα submit/update schemas, για παράδειγμα ως `nutsCode`, `nutsCodes`, `nutsCity`, `nutsPostalCode` ή `nutsCountry`. Συνεπώς η εφαρμογή εφαρμόζει τις περιοχές ως **local scoring/filtering signal** και όχι ως upstream API filter.

Πρακτική συνέπεια:

```text
Το ΚΗΜΔΗΣ API περιορίζεται με CPV, ημερομηνίες, φορείς, ποσά και ΑΔΑΜ.
Οι περιοχές NUTS αξιολογούνται μετά την ανάκτηση, μέσα στην εφαρμογή.
```

Η βαθμολόγηση περιοχής βασίζεται σε διαθέσιμα structured NUTS πεδία όταν υπάρχουν και σε ασθενέστερα text fallbacks όταν δεν υπάρχουν. Το structured NUTS match θεωρείται πιο αξιόπιστο από την απλή αναφορά γεωγραφικού όρου σε τίτλο, φορέα ή raw JSON.

---

## 5. Κλήσεις ΚΗΜΔΗΣ στην εφαρμογή

### 5.1 Γενική Αναζήτηση ΚΗΜΔΗΣ

Η οθόνη `/kimdis` είναι live search εργαλείο. Δεν αποτελεί το καθημερινό ingest. Σκοπός της είναι ο ad hoc έλεγχος του επίσημου API, η διερεύνηση συγκεκριμένων ΑΔΑΜ και η χειροκίνητη αποθήκευση αποτελεσμάτων στο επιλεγμένο προφίλ.

Τα views της οθόνης αντιστοιχούν στα παρακάτω resources:

| View | Resources | Περιγραφή |
|---|---|---|
| `opportunities` | `notice` | Ευκαιρίες συμμετοχής. Διακηρύξεις/προσκλήσεις με πιθανό ενδιαφέρον συμμετοχής. |
| `signals` | `request` | Πρώιμα σήματα πιθανής μελλοντικής ανάγκης. |
| `market` | `auction`, `contract`, `payment` | Έρευνα αγοράς, αναθέσεις, συμβάσεις και πληρωμές. |
| `advanced` | επιλεγμένο resource ή όλα | Τεχνική αναζήτηση ανά είδος πράξης. |

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

Δεν αποστέλλονται όλα τα πεδία σε όλα τα resources. Το `isModified` αποστέλλεται μόνο σε `notice`, `auction` και `contract`, επειδή τα `request` και `payment` το απορρίπτουν. Το `procedureType` περιορίζεται στα resources που το υποστηρίζουν. Τα `finalDateFrom` και `finalDateTo` εφαρμόζονται μόνο στο `notice`.

Αν ο χρήστης δώσει ΑΔΑΜ, το σύστημα κάνει infer το resource:

| Περιεχόμενο ΑΔΑΜ | Resource |
|---|---|
| `REQ` | `request` |
| `PROC` | `notice` |
| `AWRD` | `auction` |
| `SYMV` | `contract` |
| `PAY` | `payment` |

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
- keywords ή negative keywords,
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
| UI result cap | `/kimdis` και dashboard | Περιορίζει το πλήθος εμφανιζόμενων εγγραφών, όχι απαραίτητα το πλήθος που επέστρεψε το API. |
| Περιοχές NUTS | Scoring / UI filters | Δεν μειώνει το API response· επηρεάζει score ή local display. |
| Keywords | Scoring | Δεν μειώνει το API response· αυξάνει/μειώνει score μετά την ανάκτηση. |
| Budget προφίλ | Scoring | Δεν αποστέλλεται στο παραγωγικό ingest· χρησιμοποιείται στη βαθμολόγηση. |
| Deadline active/expired | Dashboard/reports filters | Δεν περιορίζει το παραγωγικό ingest· περιορίζει την προβολή. |

Με `KHMDHS_MAX_PAGES=100`, το κανονικό ingest μπορεί να ανακτήσει έως 100 σελίδες από το `notice`. Αν το API έχει περισσότερες σελίδες, η εφαρμογή καταγράφει warning `kimdis_max_pages` και διατηρεί τα αποτελέσματα που έχουν ήδη επιστραφεί.

---

## 7. Rate limiting και safe handling ΚΗΜΔΗΣ

Το επίσημο OpenData API του ΚΗΜΔΗΣ έχει όριο 350 αιτημάτων ανά λεπτό. Η εφαρμογή διαθέτει reactive μηχανισμό προστασίας για HTTP `429 Too Many Requests`.

Ρυθμίσεις:

| Μεταβλητή | Προεπιλογή | Περιγραφή |
|---|---:|---|
| `KHMDHS_PAGE_DELAY_SECONDS` | `1.0` | Καθυστέρηση μεταξύ διαδοχικών σελίδων. |
| `KHMDHS_RATE_LIMIT_RETRIES` | `4` | Πλήθος επαναλήψεων μετά από 429. |
| `KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS` | `5.0` | Βασική καθυστέρηση exponential backoff. |
| `KHMDHS_TIMEOUT_SECONDS` | `45` | Timeout ανά HTTP request. |

Η συμπεριφορά είναι η εξής:

```text
Κανονική ροή:
  page 0 → αναμονή KHMDHS_PAGE_DELAY_SECONDS → page 1 → ...

Σε HTTP 429:
  αν υπάρχει Retry-After header, χρησιμοποιείται αυτό με ασφαλές cap
  αλλιώς εφαρμόζεται exponential backoff:
    5s, 10s, 20s, 40s ... ανάλογα με τις ρυθμίσεις
  μετά το όριο retries, το τρέχον search σταματά
```

Ο μηχανισμός είναι **reactive**. Δεν υπάρχει ακόμη proactive quota counter τύπου «μετρήθηκαν 350 requests στο τελευταίο λεπτό, αναμονή μέχρι το επόμενο λεπτό». Με `KHMDHS_PAGE_DELAY_SECONDS=1.0`, ένα single ingest εκτελεί περίπου 60 requests/minute για paginated search, δηλαδή αρκετά χαμηλότερα από το επίσημο όριο. Παρ’ όλα αυτά, ταυτόχρονες ενέργειες, όπως manual searches, scheduler, `adamChain` calls και PDF downloads, μπορούν να αυξήσουν το συνολικό φορτίο.

Όταν ο client φτάσει σε rate limit μετά τα retries, καταγράφεται `kimdis_rate_limit` στα system events και επιστρέφονται/αποθηκεύονται όσα αποτελέσματα είχαν ήδη ανακτηθεί.

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
| `GET` | `/reports` | HTML | Σελίδα αναφορών ανά προφίλ και φίλτρα. |
| `GET` | `/reports/export` | File response | Εξαγωγή αναφοράς σε PDF, CSV, JSONL ή Markdown. |
| `GET` | `/profiles/{profile_id}/export` | File response | Εξαγωγή προφίλ σε PDF ή Markdown. |
| `GET` | `/api/cpv/search` | JSON | Αναζήτηση CPV από τον τοπικό κατάλογο. |
| `GET` | `/api/cpv/children` | JSON | Ανάκτηση παιδιών CPV από τον τοπικό κατάλογο. |
| `GET` | `/maintenance` | HTML | Τεχνική σελίδα συντήρησης και usage summary. |
| `GET` | `/activity` | HTML | System event log. |
| `GET` | `/api/tenders` | JSON | Περιορισμένο JSON endpoint για scores άνω του `min_score`. |

---

## 9. Μοντέλο δεδομένων

### 9.1 `client_profiles`

Αποθηκεύει προφίλ ενδιαφέροντος. Περιλαμβάνει CPV, prefixes, keywords, negative keywords, preferred regions, budget range, required certificates, RSS feeds και ενεργή/ανενεργή κατάσταση.

### 9.2 `tenders`

Κοινή αποθήκη πράξεων. Το μοναδικότητα ορίζεται από `source + source_reference`. Περιλαμβάνει ΑΔΑΜ, τίτλο, φορέα, ημερομηνίες, ποσά, CPV, official URL, attachment URL, raw JSON, PDF text και ingest markers.

### 9.3 `tender_scores`

Πίνακας συσχέτισης διαγωνισμού με προφίλ. Περιλαμβάνει `score`, `rule_score`, matched CPV/keywords, reasons, recommended action, workflow status, user notes και profile-specific latest ingest markers. Υπάρχει unique constraint `tender_id + profile_id`.

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

Ενδεικτική ερμηνεία:

| Score | Ερμηνεία | Recommended action |
|---:|---|---|
| `0–54` | Χαμηλή σχετικότητα. | `ignore` |
| `55–74` | Πιθανή ευκαιρία / μεσαία προτεραιότητα. | `review` |
| `75–100` | Υψηλή προτεραιότητα. | `bid` |

### 10.1 Μοντέλο rule-based βαθμολόγησης

Η βασική βαθμολόγηση είναι adaptive rule-based. Δεν χρησιμοποιείται σταθερός παρονομαστής για όλα τα προφίλ. Στον παρονομαστή συμμετέχουν μόνο τα κριτήρια που έχουν οριστεί στο προφίλ και για τα οποία υπάρχουν επαρκή δεδομένα στον διαγωνισμό. Αυτό αποτρέπει την τεχνητή υποβάθμιση προφίλ που έχουν, για παράδειγμα, μόνο CPV χωρίς budget ή περιοχές.

Η rule-based βαθμολογία υπολογίζεται με την ακόλουθη λογική:

```text
rule_score = (positive_points / available_points) * 85
             + keyword_bonus
             + deadline_bonus
             + penalties

Το αποτέλεσμα περιορίζεται στο διάστημα 0–100.
```

Η τιμή `85` είναι το adaptive maximum των θετικών rule-based κριτηρίων πριν από bonuses/penalties. Ένας ενεργός διαγωνισμός μπορεί να λάβει επιπλέον `+10` λόγω μελλοντικής προθεσμίας. Για τον λόγο αυτό, ένα καθαρό CPV-only leaf match με μελλοντική προθεσμία μπορεί να φτάσει περίπου στο `95`, όχι αυτόματα στο `100`.

### 10.2 Θετικά κριτήρια και βάρη

| Κριτήριο | Βάρος | Πότε συμμετέχει στον παρονομαστή | Παρατηρήσεις |
|---|---:|---|---|
| CPV | `45` | Όταν το προφίλ έχει `cpv_codes` ή `cpv_prefixes`. | Το μεγαλύτερο βάρος. Προσαρμόζεται από specificity και coverage factors. |
| Budget | `12` | Όταν το προφίλ έχει min/max budget και ο διαγωνισμός έχει διαθέσιμο ποσό χωρίς ΦΠΑ. | Αν λείπει ποσό, παραμένει ουδέτερο. |
| Περιοχή | `10` | Όταν το προφίλ έχει preferred regions και ο διαγωνισμός έχει διαθέσιμο γεωγραφικό σήμα. | Structured NUTS match είναι ισχυρότερο από text fallback. |
| Απαιτήσεις / πιστοποιητικά | `8` | Όταν έχουν δηλωθεί απαιτήσεις και υπάρχει επαρκές κείμενο για έλεγχο. | Πριν από PDF analysis συνήθως παραμένει ουδέτερο. |

Τα βάρη δεν αποτελούν απλή πρόσθεση μέχρι το 100. Κανονικοποιούνται δυναμικά στο `ADAPTIVE_MAX_POINTS = 85`, ανάλογα με τα διαθέσιμα και εφαρμόσιμα κριτήρια.

### 10.3 CPV scoring

Το CPV αποτελεί το κύριο κριτήριο σχετικότητας, αλλά το exact match δεν είναι πάντα ισοδύναμο. Η εφαρμογή διακρίνει ειδικά leaf CPV από γενικούς parent CPV και descendant matches.

| Περίπτωση CPV match | Συντελεστής ισχύος |
|---|---:|
| Exact match σε ειδικό/leaf CPV | `1.00` |
| Exact match σε parent CPV επιπέδου 0 | `0.72` |
| Exact match σε parent CPV επιπέδου 1 | `0.85` |
| Exact match σε parent CPV επιπέδου 2 ή υψηλότερο | `0.92` |
| Descendant match από πολύ γενικό selected parent | `0.60` |
| Descendant match από selected parent επιπέδου 1 | `0.72` |
| Descendant match από πιο ειδικό selected parent | `0.82` |
| Prefix/family match | `0.70` |

Σε διαγωνισμούς με πολλαπλά CPV εφαρμόζεται ήπιος coverage factor:

```text
coverage_factor = 0.85 + 0.15 * (matched_cpv_count / total_cpv_count)
```

Αν ένας διαγωνισμός έχει πολλά CPV και ταιριάζει μόνο ένα από αυτά, παραμένει ορατός, αλλά αντιμετωπίζεται ως μικτό/μερικό CPV match. Αν δεν βρεθεί κανένα CPV που να ταιριάζει με το προφίλ, εφαρμόζεται penalty `-12`.

Τα πολύ γενικά parent CPV, όπως `33000000-0`, χρησιμοποιούνται κυρίως για broad monitoring και discovery. Αυξάνουν την κάλυψη ingest μέσω CPV expansion, αλλά δεν πρέπει να οδηγούν από μόνα τους σε υψηλή προτεραιότητα χωρίς επιπλέον ενδείξεις από budget, περιοχή, keywords, PDF ή άλλα στοιχεία.

### 10.4 Budget scoring

Το budget αξιολογείται μόνο όταν ο διαγωνισμός παρέχει χρησιμοποιήσιμο ποσό χωρίς ΦΠΑ.

| Περίπτωση | Επίδραση |
|---|---:|
| Ποσό εντός min/max ορίων προφίλ | `+12` |
| Ποσό κάτω από το ελάχιστο | `-8` |
| Ποσό πάνω από το μέγιστο | `-12` |
| Μη διαθέσιμο ποσό | ουδέτερο |

Το μη διαθέσιμο ποσό θεωρείται έλλειψη δεδομένων και όχι αρνητική ένδειξη.

### 10.5 Region / NUTS scoring

Οι περιοχές δεν αποστέλλονται ως φίλτρο στο ΚΗΜΔΗΣ OpenData search. Αξιολογούνται τοπικά μετά την ανάκτηση, με βάση structured NUTS/raw γεωγραφικά πεδία και δευτερευόντως text fallbacks.

| Περίπτωση | Επίδραση |
|---|---:|
| Ισχυρό structured region/NUTS match | `+10` |
| Ασθενές text-based γεωγραφικό match | `+6` |
| Υπάρχει γεωγραφικό σήμα αλλά δεν ταιριάζει με το προφίλ | `-6` |
| Δεν υπάρχουν επαρκή γεωγραφικά στοιχεία | ουδέτερο |

Η διάκριση strong/weak match έχει σκοπό να μειώσει false positives, π.χ. περιπτώσεις όπου ένας όρος περιοχής εμφανίζεται σε κείμενο χωρίς να δηλώνει πραγματικό τόπο εκτέλεσης.

### 10.6 Keywords και negative keywords

Οι θετικές λέξεις-κλειδιά λειτουργούν κυρίως ως bonus και όχι ως υποχρεωτικό κριτήριο, ώστε ένα ισχυρό CPV match να μην καταρρέει επειδή δεν βρέθηκε μία προαιρετική λέξη.

| Περίπτωση | Επίδραση |
|---|---:|
| 1 θετικό keyword | έως `+5.6` bonus |
| 2 θετικά keywords | έως `+7.2` bonus |
| 3+ θετικά keywords | έως `+8.0` bonus |
| Keyword-only profile | adaptive base βάρος `45` |
| Αρνητικές λέξεις | penalty `-12` ανά match, έως `-35` |

Σε keyword-only προφίλ, όπου δεν υπάρχουν CPV/budget/περιοχές/απαιτήσεις, τα keywords γίνονται το βασικό κριτήριο με βάρος `45`, ώστε να μπορεί να λειτουργήσει και προφίλ καθαρής κειμενικής αναζήτησης.

### 10.7 Απαιτήσεις, πιστοποιητικά και PDF text

Οι απαιτήσεις/πιστοποιητικά αξιολογούνται κυρίως όταν υπάρχει αναλυμένο κείμενο PDF ή άλλο επαρκές διαθέσιμο κείμενο.

| Περίπτωση | Επίδραση |
|---|---:|
| Εντοπίζονται όλες οι απαιτήσεις | `+8` |
| Υπάρχει PDF/text και λείπουν απαιτήσεις | penalty έως `-25`, με `-8` ανά έλλειψη |
| Δεν έχει γίνει PDF analysis | ουδέτερο |

Η ουδέτερη συμπεριφορά πριν από το PDF analysis αποτρέπει ψευδείς αρνητικές βαθμολογίες όταν τα σχετικά κριτήρια βρίσκονται μόνο μέσα στη διακήρυξη.

### 10.8 Deadline, cancellation και τελικές ποινές

| Περίπτωση | Επίδραση |
|---|---:|
| Μελλοντική καταληκτική ημερομηνία | `+10` |
| Παρελθούσα καταληκτική ημερομηνία | `-35` |
| Ματαιωμένη/ακυρωμένη πράξη | `-60` |

Οι παραπάνω παράγοντες εφαρμόζονται στο τελικό score μετά την adaptive κανονικοποίηση των θετικών κριτηρίων.

### 10.9 Τελικό score

Το τελικό score είναι το rule-based score του προφίλ. Αν γίνει ανάλυση PDF και αποθηκευτεί extracted text, το ίδιο rule-based scoring μπορεί να ξανατρέξει με περισσότερα διαθέσιμα κείμενα, χωρίς χρήση εξωτερικού μοντέλου.

### 10.10 Ενδεικτικές συνέπειες της λογικής

- Exact CPV match σε ειδικό leaf code και μελλοντική προθεσμία μπορεί να οδηγήσει σε πολύ υψηλό score, περίπου έως `95`, εφόσον δεν υπάρχουν αρνητικές ενδείξεις.
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

## 12. PDF analysis

Η ανάλυση PDF εκτελείται από το `/tenders/{tender_id}/analyze-pdf`. Το σύστημα ανακτά το official attachment από το ΚΗΜΔΗΣ και εξάγει ενσωματωμένο κείμενο PDF.

Περιορισμοί:

- Δεν εκτελείται OCR σε scanned PDFs.
- Η εξαγωγή εξαρτάται από το αν το PDF περιέχει selectable/embedded text.
- Το καθημερινό ingest δεν κατεβάζει μαζικά PDFs, εκτός αν ενεργοποιηθεί ρητά `AUTO_FETCH_PDF_TEXT=true`.
- Μετά την εξαγωγή, ο διαγωνισμός επαναβαθμολογείται για τα σχετικά προφίλ.

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

---

## 14. Reports

Οι αναφορές βασίζονται στον πίνακα `tender_scores` και είναι profile-oriented. Υποστηρίζουν φίλτρα προφίλ, περιόδου, score, ενεργής/ληγμένης προθεσμίας, αναζήτησης και περιοχής.

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

Οι αναφορές περιλαμβάνουν πλέον το πλαίσιο του επιλεγμένου προφίλ: όνομα, αποθηκευμένη περιγραφή επιχείρησης/δυνατοτήτων, CPV, prefixes, keywords, negative keywords, απαιτήσεις, NUTS και εύρος προϋπολογισμού.

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
| `KHMDHS_PAGE_DELAY_SECONDS` | Καθυστέρηση μεταξύ σελίδων ΚΗΜΔΗΣ. |
| `KHMDHS_RATE_LIMIT_RETRIES` | Retries μετά από HTTP 429. |
| `KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS` | Βάση exponential backoff μετά από 429. |
| `ENABLE_DIAVGEIA_RSS` | Legacy/optional RSS ingest flag. Default false. |
| `DIAVGEIA_BASE_URL` | Base URL Διαύγειας για OpenData calls. |
| `DIAVGEIA_TIMEOUT_SECONDS` | Timeout ανά Διαύγεια request. |
| `DIAVGEIA_DEFAULT_PAGE_SIZE` | Default μέγεθος σελίδας σε Διαύγεια searches. |
| `SCHEDULE_HOUR` / `SCHEDULE_MINUTE` | Ώρα ημερήσιου scheduler. |
| `INGEST_DAYS_BACK` | Παράθυρο ημερών για ΚΗΜΔΗΣ ingest. |
| `MATCH_THRESHOLD` | Default όριο σχετικότητας για dashboard/reports. |
| `FETCH_PDF_FOR_SCORE_ABOVE` | Threshold για προαιρετική αυτόματη λήψη PDF text. |
| `AUTO_FETCH_PDF_TEXT` | Αν είναι true, επιτρέπει μαζικό PDF fetch στο ingest υπό προϋποθέσεις. Default false. |
| `APP_TIMEZONE` | Ζώνη ώρας εμφάνισης και reports. |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Προαιρετικό Basic Auth για web interface. |
| `SMTP_*`, `DIGEST_RECIPIENTS` | Προαιρετικό email digest. |

Παράδειγμα παραγωγικής ρύθμισης ΚΗΜΔΗΣ με ήπιο request pacing:

```env
KHMDHS_TIMEOUT_SECONDS=45
KHMDHS_MAX_PAGES=100
KHMDHS_PAGE_DELAY_SECONDS=1.0
KHMDHS_RATE_LIMIT_RETRIES=4
KHMDHS_RATE_LIMIT_BASE_DELAY_SECONDS=5.0
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

Για demo χρήση μπορεί να παραμείνει κενό το Basic Auth. Για χρήση από πελάτη πρέπει να οριστούν `ADMIN_USERNAME` και `ADMIN_PASSWORD`.

Η εφαρμογή λειτουργεί με rule-based scoring και δεν απαιτεί εξωτερικό μοντέλο.

---

## 18. Γνωστοί περιορισμοί

| Περιορισμός | Επίδραση |
|---|---|
| Δεν υπάρχει upstream NUTS filter στο τεκμηριωμένο ΚΗΜΔΗΣ `notice` search | Οι περιοχές εφαρμόζονται μετά την ανάκτηση, όχι στο API request. |
| Το rate limit handling είναι reactive | Υπάρχει backoff σε 429, αλλά όχι proactive request counter ανά λεπτό. |
| Δεν υπάρχει OCR | Scanned PDFs δεν αποδίδουν αξιόπιστο κείμενο. |
| Δεν γίνεται μαζικό PDF download default | Το scoring πριν το PDF analysis βασίζεται σε metadata. |
| Πολύ γενικά parent CPV μπορούν να επιστρέψουν μεγάλο όγκο | Επηρεάζονται από `KHMDHS_MAX_PAGES`, date window και rate limits. |
| Διαύγεια labels δεν είναι πλήρως resolved | Αποθηκεύονται IDs όταν το API δεν επιστρέφει readable names. |
| Το Διαύγεια enrichment δεν επηρεάζει score | Παρέχει evidence/context, όχι decision automation. |

---

## 19. Πιθανά επόμενα τεχνικά βήματα

| Πεδίο | Περιγραφή |
|---|---|
| Proactive ΚΗΜΔΗΣ limiter | Προσθήκη `KHMDHS_REQUESTS_PER_MINUTE` και shared request window counter. |
| Local NUTS filter | Ρητό post-fetch φίλτρο περιοχής με διάκριση structured NUTS vs weak text fallback. |
| Διαύγεια dictionaries | Lookup/cache για organization names, decision type labels, units και signers. |
| OCR | Προαιρετική υποστήριξη OCR για scanned PDFs. |
| ΚΗΜΔΗΣ market intelligence refinements | Περαιτέρω αξιοποίηση των ΚΗΜΔΗΣ `request`, `auction`, `contract` και `payment` resources για αναλύσεις φορέων, αναδόχων, ποσών και συχνότητας. |
| Migration framework | Εισαγωγή Alembic για ελεγχόμενες αλλαγές schema. |

---

## 20. Changelog

### v0.10.6

- Διευκρινίστηκε στο προϊόν και στο README ότι η Διαύγεια λειτουργεί ως secondary evidence layer, όχι ως δεύτερη κύρια μηχανή market intelligence.
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
