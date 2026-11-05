# Veille du calendrier — détection de changement de page

Service Python autonome qui surveille une page de calendrier publique, compare
son contenu normalisé à une référence figée et signale un changement lorsqu'au
moins une règle indépendante se déclenche. Il expose aussi une interface
opérateur en français pour consulter les résultats, gérer les règles et refiger
la référence.

Le service n'utilise que la bibliothèque standard de Python 3.12 : aucune
dépendance externe, aucun framework, aucune exécution de code ou de script issu
de la page surveillée.

## Démarrage

```bash
cp config/app.example.json config/app.json
# adapter config/app.json, en particulier target_url, database_path et user_agent
python3 -m app
```

Le serveur de développement écoute par défaut sur `http://127.0.0.1:8080`.
`Ctrl+C` arrête proprement le serveur et le planificateur en laissant un
contrôle en cours se terminer.

Le fichier `config/app.json` et le répertoire `var/` sont ignorés par Git.
Le schéma SQLite est créé ou complété de façon idempotente au démarrage ; les
contrôles, résultats par règle, références et refigements survivent aux
redémarrages. Les migrations de détection sont suivies dans la table
`detection_schema_migrations` : le service ne modifie jamais `PRAGMA
user_version`, qui reste disponible pour un autre service partageant le même
fichier.

### Tests

```bash
python3 -m unittest -q
```

Les tests couvrent la configuration, le schéma, la sécurité HTTP, la
normalisation, la récupération réseau, les règles, la boucle de contrôle,
l'administration des règles, le refigement, le tableau de bord et la
documentation. Aucun test n'accède à un hôte externe : les scénarios réseau
utilisent un serveur local sur `127.0.0.1`.

## Configuration

La précédence est : valeurs par défaut, puis fichier JSON, puis variables
d'environnement. Le chemin du fichier peut être forcé par `DETECTION_CONFIG` ou
par l'argument `config_path` de `load_config`. Une configuration invalide
empêche le démarrage et liste toutes les erreurs détectées.

| Clé | Variable d'environnement | Défaut | Rôle |
| --- | --- | --- | --- |
| `target_url` | `DETECTION_TARGET_URL` | `https://example.org/` | Page HTTP(S) surveillée, sans identifiants dans l'URL |
| `interval_seconds` | `DETECTION_INTERVAL_SECONDS` | `900` | Délai fixe entre deux contrôles |
| `request_timeout_seconds` | `DETECTION_REQUEST_TIMEOUT_SECONDS` | `15` | Budget total d'une récupération, redirections comprises |
| `max_response_bytes` | `DETECTION_MAX_RESPONSE_BYTES` | `2097152` | Taille maximale du corps accepté |
| `user_agent` | `DETECTION_USER_AGENT` | `DetectionCalendrier/1.0 (+contact: …)` | En-tête d'identification explicite et stable |
| `database_path` | `DETECTION_DATABASE_PATH` | `var/detection.sqlite3` | Fichier SQLite |
| `operator_label` | `DETECTION_OPERATOR_LABEL` | `opérateur` | Opérateur affiché et enregistré |
| `max_request_bytes` | `DETECTION_MAX_REQUEST_BYTES` | `1048576` | Taille maximale d'un formulaire opérateur |
| `host` | `DETECTION_HOST` | `127.0.0.1` | Interface d'écoute du serveur de développement |
| `port` | `DETECTION_PORT` | `8080` | Port d'écoute |

`config/app.example.json` contient un exemple complet et valide. Remplacez
l'adresse de contact du `user_agent` avant tout déploiement réel.

## Points d'entrée HTTP

| Méthode | Chemin | Usage |
| --- | --- | --- |
| `GET` | `/health` | Sonde JSON : `200` avec `{"status":"ok"}` ou `503` si le schéma n'est pas interrogeable |
| `GET` | `/` | Tableau de bord : état du service, cible, référence, dernier contrôle et résultats par règle, historique récent, refigements |
| `GET` | `/checks/<id>` | Détail d'un contrôle passé : résultat, motif, catégorie d'échec, métadonnées HTTP, résultats par règle avec extraits |
| `GET` | `/rules` | Liste des règles et de leurs paramètres |
| `GET` | `/rules/new` | Formulaire d'ajout d'une règle |
| `POST` | `/rules` | Ajout d'une règle |
| `GET` | `/rules/<id>/edit` | Formulaire de modification |
| `POST` | `/rules/<id>` | Mise à jour du nom, des paramètres et de l'état actif |
| `POST` | `/rules/<id>/enable` | Activation d'une règle |
| `POST` | `/rules/<id>/disable` | Désactivation d'une règle |
| `GET` | `/reference/refreeze` | Confirmation affichant la cible, la référence actuelle et l'historique des refigements |
| `POST` | `/reference/refreeze` | Refigement confirmé (`confirm=yes`) et protégé par CSRF |

Tous les formulaires `POST` utilisent une protection CSRF à double soumission :
le jeton `detection_csrf` (cookie `HttpOnly`, `SameSite=Lax`) doit être renvoyé
dans le champ `csrf_token`. Le type de contenu doit être
`application/x-www-form-urlencoded` et la requête ne doit pas dépasser
`max_request_bytes`. Les réponses portent des en-têtes de sécurité
conservateurs (`Content-Security-Policy`, `X-Content-Type-Options`,
`X-Frame-Options`, etc.) et `Cache-Control: no-store`.

## Règles de détection

Chaque règle est persistée indépendamment avec un identifiant stable, un nom
unique, un type, un état actif et des paramètres validés. Les règles actives
sont évaluées séparément puis combinées en **OU** : une seule règle déclenchée
suffit à qualifier le contrôle de `changed`. Une règle inactive est signalée
`disabled` ; une règle qui ne peut pas comparer sa référence est
`not_evaluated`.

Trois types sont disponibles :

### `closure_block` — bloc d'annonce de fermeture

Paramètre `anchor` : texte spécifique qui identifie le bloc d'annonce
(par défaut `Complet 2026`). La règle se déclenche si le bloc contenant cette
ancre est modifié ou disparaît.

### `update_timestamp` — horodatage de mise à jour

Paramètres `label` (par défaut `mise à jour`) et `window` (par défaut `160`
caractères, maximum `2000`). La règle se déclenche si la date/heure affichée à
proximité du libellé est modifiée ou disparaît.

### `new_element` — élément absent de la référence

Paramètre `sources` : sous-ensemble de `document`, `link`, `time`. La règle se
déclenche lorsqu'un lien HTTP(S), un document daté ou un élément `<time>`
présent dans la page n'existe pas dans la référence.

Les règles par défaut sont les trois règles ci-dessus, avec les paramètres
indiqués. Aucune règle par défaut ne teste la seule présence des mots
« rendez-vous » ou « calendrier » : la page fermée contient déjà ce vocabulaire,
donc une telle règle se déclencherait immédiatement. Les paramètres génériques
de ce type sont explicitement refusés par la validation.

L'opérateur peut ajouter une règle d'un type pris en charge, modifier son nom,
ses paramètres et son état, ou l'activer/désactiver depuis `/rules`. Une
modification prend effet au contrôle suivant ; elle ne touche jamais la
référence figée et ne crée ni contrôle ni signal de changement. La référence
conserve une structure de blocs indépendante des paramètres : l'ancre, le
libellé et la fenêtre courants sont réappliqués à la référence comme à la page
au moment de chaque évaluation.

## Normalisation du contenu

Le HTML est analysé uniquement avec `html.parser`. Les commentaires, `script`,
`style`, `noscript`, `template`, `head` et les champs cachés sont exclus du
contenu visible. Le texte est normalisé en Unicode NFC et les espaces sont
compactés. La référence stockée est un instantané canonique déterministe :

- texte visible, un bloc par ligne ;
- blocs de fermeture candidats contenant l'ancre configurée ;
- horodatages trouvés près du libellé configuré, dans la fenêtre bornée ;
- empreintes stables des liens HTTP(S), documents datés et éléments `<time>`.

Deux pages identiques produisent un JSON octet pour octet identique et la même
empreinte SHA-256. Le contenu brut n'est jamais stocké, renvoyé à l'interface ou
journalisé ; seuls l'instantané normalisé et des extraits bornés (160 caractères,
une ligne) sont conservés.

## Résultats de contrôle

Un contrôle produit exactement l'un des résultats suivants :

- `unchanged` : comparaison réussie, aucune règle active déclenchée ;
- `changed` : au moins une règle active déclenchée ;
- `control_failure` : la page n'a pas pu être récupérée ou validée.

Le premier contrôle réussi sans référence enregistre la référence et produit
`unchanged` avec le motif `reference_initialized`. Chaque contrôle enregistre un
résultat par règle (`triggered`, `not_triggered`, `disabled` ou
`not_evaluated`) avec l'extrait actuel et l'extrait de référence concernés.

Un échec de contrôle n'est **jamais** un changement de contenu : aucune règle
n'est marquée déclenchée, aucun signal de changement n'est émis et la référence
reste utilisable. Les catégories d'échec sont :

| Catégorie | Signification |
| --- | --- |
| `network_error` | Connexion impossible, DNS en échec, connexion refusée |
| `timeout` | Budget total de récupération dépassé |
| `http_status` | Réponse HTTP non 2xx |
| `empty_body` | Corps vide |
| `unsupported_content_type` | Type différent de `text/html` ou `application/xhtml+xml` |
| `oversized_response` | Corps supérieur à `max_response_bytes` |
| `unparseable_content` | Corps indécodable, page sans contenu visible exploitable ou instantané de référence illisible |

## Discipline de récupération

Chaque cycle effectue une seule requête logique `GET` sur l'URL configurée :

- en-tête `User-Agent` explicite et stable ;
- délai total configurable, redirections comprises ;
- au plus cinq redirections HTTP(S) ;
- aucun cookie, aucune authentification, aucune reprise automatique ;
- taille et type de contenu contrôlés avant analyse ;
- aucune URL trouvée dans la page n'est suivie.

Le planificateur utilise un délai fixe (900 s par défaut) et un verrou non
bloquant : deux contrôles ne se chevauchent jamais. Les journaux structurés
contiennent uniquement des identifiants, statuts, catégories, compteurs et
durées, jamais le contenu de la page ni les extraits.

## Refigement de la référence

Le refigement est une action explicite de l'opérateur :

1. `GET /reference/refreeze` affiche la cible, l'empreinte, la date et
   l'opérateur de la référence actuelle, un avertissement de remplacement et
   l'historique.
2. `POST /reference/refreeze` exige le jeton CSRF et `confirm=yes`. Sans
   confirmation, aucune récupération n'a lieu.
3. En cas de succès, une récupération fraîche et validée remplace atomiquement
   la référence : l'ancienne est conservée comme historique, la nouvelle est
   insérée et un événement de réussite est écrit dans la même transaction.
4. En cas d'échec de récupération, seule une entrée d'échec est ajoutée ;
   la référence actuelle reste en place et utilisable. Aucun contrôle ni aucun
   acquittement d'épisode n'est créé par le refigement.
5. Le contrôle suivant, sur un contenu identique, est `unchanged`, ce qui
   remet la comparaison à zéro.

L'historique des refigements est visible sur la page de confirmation et sur le
tableau de bord.

## Contenu non fiable

La page surveillée est une donnée non fiable, jamais une instruction. Le service
ne l'exécute pas, ne l'interprète pas sémantiquement, ne suit pas ses liens et ne
l'injecte jamais telle quelle dans une page HTML. Les valeurs dynamiques de
l'interface (noms de règles, messages, extraits, métadonnées) sont échappées
avant rendu, et les extraits sont compactés et bornés. Les journaux excluent par
construction les champs de contenu connus.

## Vérification manuelle rapide

1. Servir localement `tests/fixtures/closed_page.html` (par exemple avec
   `python3 -m http.server` dans le répertoire des fixtures).
2. Configurer `target_url` vers cette page et démarrer le service.
3. Attendre ou déclencher le premier contrôle : le tableau de bord doit afficher
   `Aucun changement` et `Référence initiale capturée`.
4. Modifier la copie locale (par exemple remplacer `Complet 2026` par
   `Ouvert 2027`) : le contrôle suivant doit afficher `Changement détecté` avec
   la règle `closure_block` déclenchée et l'extrait concerné.
5. Arrêter le serveur local : le contrôle suivant doit afficher
   `Échec du contrôle` (`network_error`), sans changement détecté.
6. Refiger la référence depuis `/reference/refreeze` puis relancer un contrôle
   sur un contenu identique : il doit être `unchanged`.
