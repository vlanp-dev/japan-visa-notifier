# Service d'alertes d'épisodes

Service HTTP minimal, en Python 3 et bibliothèque standard uniquement, destiné à
recevoir des épisodes de changement et à journaliser leurs tentatives de
livraison. Le socle fournit :

- chargement de configuration JSON avec surcharges par variables d'environnement
  et masquage des secrets ;
- base SQLite et dépôts pour les épisodes et les tentatives de livraison
  (`email` ou `sms`, type `alert`, `resend` ou `test`, destinataire masqué,
  nombre de segments) ;
- application WSGI avec routes, formulaires protégés par CSRF, pages HTML
  échappées et en-têtes de sécurité ;
- journaux applicatifs, endpoint de santé et serveur de développement ;
- canal email obligatoire : composition française, envoi SMTP ou boîte de
  sortie locale, journalisation des succès et des échecs, réémission email ;
- canal SMS facultatif : adaptateur HTTP-JSON générique, vérification explicite
  par un test depuis l'interface, message français d'un seul segment et
  journalisation partagée avec l'email.

Aucun fournisseur SMS n'est fourni ni sélectionné par défaut. Sans section
`sms` (ou avec une section vide), le canal est explicitement inactif et le
service fonctionne normalement en email seul. Aucun appel réseau n'est effectué
au démarrage.

## Démarrage

```bash
cp config/app.example.json config/app.json   # puis adapter les valeurs
python3 -m app
```

Sans fichier `config/app.json`, le service démarre avec les valeurs par défaut
(serveur sur `127.0.0.1:8000`, base `var/alerts.sqlite3`) et un secret de
signature éphémère. Le fichier de configuration peut aussi être indiqué par
`--config` ou `ALERTS_CONFIG_FILE`.

- Tableau de bord : <http://127.0.0.1:8000/>
- Nouvel épisode : <http://127.0.0.1:8000/episodes/new>
- Journal des livraisons : <http://127.0.0.1:8000/deliveries>
- Santé JSON : <http://127.0.0.1:8000/health> (alias `/healthz`)

Point d'entrée WSGI pour un serveur de production :

```bash
gunicorn wsgi:application
```

## Configuration

Le document JSON comporte les sections `service`, `server`, `database`,
`logging`, `email` et `sms`. Les clés inconnues sont ignorées.

Chaque valeur peut être surchargée par une variable d'environnement préfixée par
`ALERTS__`, avec `__` pour la profondeur :

```bash
ALERTS__SERVER__PORT=9000
ALERTS__SERVER__SECRET=un-secret-aleatoire-de-32-caracteres
ALERTS__EMAIL__SMTP_HOST=smtp.example.org
ALERTS__EMAIL__PASSWORD=...
```

`server.secret` sert à signer les jetons CSRF. En production, il doit être
fourni explicitement (au moins 16 caractères) afin que les sessions survivent
aux redémarrages. En son absence, un secret éphémère est généré et un
avertissement est journalisé.

`service.base_url` est l'URL publique du service utilisée dans les liens des
emails (page de l'épisode et acquittement). Par défaut elle vaut
`http://<server.host>:<server.port>` ; en production, renseignez-la explicitement.

La section `email` est facultative, mais le canal email est le canal obligatoire.
Deux fournisseurs sont possibles via `email.provider` :

- `smtp` (valeur par défaut) : exige `smtp_host`, `sender` et `recipient` ;
  `username` et `password` activent l'authentification, `use_starttls` vaut
  `true` par défaut ;
- `outbox` : écrit chaque message en `.eml` dans `outbox_path` et exige
  `outbox_path`, `sender` et `recipient`.

Si la section `email` est incomplète, le service démarre quand même : le tableau de bord
indique « Non configuré » et chaque création d'épisode enregistre une tentative
email en échec explicite. Une panne d'envoi n'empêche ni l'acquittement ni la
réémission de l'épisode. Le mot de passe n'est jamais affiché par la
configuration masquée ni journalisé.

### Canal SMS facultatif

Le SMS est un canal facultatif. Il est désactivé tant que l'opérateur n'a pas
renseigné **et vérifié** un fournisseur. Aucun fournisseur n'est imposé par le
service : le choix d'un fournisseur **gratuit**, la vérification de ses
conditions d'utilisation, de ses quotas et de sa couverture relèvent de
l'opérateur. Aucun fournisseur payant n'entre dans le périmètre et aucun
repli payant n'est effectué. Le canal ne prend en charge qu'un seul
destinataire, sans routage international au-delà du besoin de l'opérateur.

Le service fournit un adaptateur HTTP-JSON générique : changer de fournisseur
gratuit ne demande qu'une modification de configuration, sans toucher à la
logique d'épisode ni de détection. Le fournisseur doit exposer une API HTTP
JSON acceptant le destinataire et le message.

Une section `sms` absente ou vide (`"sms": {}`) laisse le canal inactif. Si la
section est renseignée, les clés `provider`, `url`, `sender`, `recipient` et
`credential` sont obligatoires ensemble ; une section partielle est refusée au
démarrage. Les autres clés sont :

- `method` : `POST` (par défaut), `PUT` ou `PATCH` ;
- `auth_mode` : `bearer` (par défaut), `header` ou `query` ;
- `auth_header` : nom de l'en-tête d'authentification (par défaut
  `Authorization`) pour les modes `bearer` et `header` ;
- `auth_query_param` : nom du paramètre de requête pour le mode `query`
  (par défaut `api_key`) ;
- `body_template` : objet ou tableau JSON contenant au minimum `{{to}}` et
  `{{message}}`, et éventuellement `{{sender}}`. Vide, un corps JSON générique
  est utilisé ;
- `timeout_seconds` : délai maximal de l'appel, entre 0,1 et 60 secondes ;
- `max_response_bytes` : taille maximale de la réponse lue, entre 256 et
  1 048 576 octets ;
- `success_json_field` / `success_json_value` : chemin JSON (séparé par des
  points) et valeur attendue pour confirmer l'envoi. Sans
  `success_json_field`, tout statut HTTP 2xx est considéré comme un succès.

Exemple de surcharge par l'environnement, sans écrire le secret dans un
fichier :

```bash
ALERTS__SMS__PROVIDER=fournisseur-gratuit
ALERTS__SMS__URL=https://sms.example.org/api/send
ALERTS__SMS__SENDER=Alertes
ALERTS__SMS__RECIPIENT=+33600000000
ALERTS__SMS__CREDENTIAL=...
```

Le secret SMS n'est jamais rendu dans l'interface ni stocké en base : il reste
dans le fichier de configuration ou l'environnement, et le destinataire n'est
affiché que masqué.

#### Message et comportement d'envoi

Le message est rédigé en français et tient dans un seul SMS de 160 septets
GSM-7. Il contient toujours la nature du changement et le lien absolu vers la
page de l'épisode. Le lien d'acquittement n'est ajouté que si le message
complet tient dans ce segment ; sinon il est omis et le message est envoyé sans
lui. Si la nature et le lien de la page ne tiennent pas ensemble, aucun SMS
n'est envoyé : la tentative est enregistrée en échec. Le message n'est jamais
découpé et aucun contenu obligatoire n'est silencieusement supprimé.

À l'ouverture d'un épisode, l'email obligatoire est toujours tenté et
enregistré en premier. Ensuite seulement, si le canal SMS est actif, une
tentative SMS distincte est effectuée. Un canal non configuré ou non vérifié ne
fait aucun appel. Toute erreur, expiration ou quota SMS est contenu, journalisé
et affiché, sans modifier la tentative email, sans empêcher l'acquittement et
sans bloquer la réémission. La réémission reste exclusivement email.

#### États affichés

Le tableau de bord et le détail de l'épisode affichent les tentatives SMS dans
le journal de livraison partagé (fournisseur, destinataire masqué, nombre de
segments, statut, erreur expurgée). Les envois et échecs sont aussi journalisés
sous le logger `sms`. Les quatre états explicites du canal sont :

- **Inactif — non configuré** : aucune configuration SMS exploitable ; le
  service fonctionne normalement en email seul.
- **Inactif — non vérifié** : configuration présente mais aucun test réussi
  depuis la dernière modification de la configuration.
- **Actif et vérifié** : un test a réussi avec la configuration courante ; les
  prochains épisodes déclenchent une tentative SMS.
- **Actif mais en échec** : le canal reste actif et sera retenté, mais le
  dernier envoi réel a échoué ; l'erreur est affichée.

#### Procédure de vérification

1. Renseigner la section `sms` avec un fournisseur gratuit vérifié, puis
   démarrer le service.
2. Le tableau de bord doit afficher **Inactif — non vérifié**.
3. Cliquer sur **Envoyer un SMS de test**. Le bouton utilise une requête
   protégée par CSRF et ne crée aucun épisode.
4. En cas de succès, le bandeau indique « Test SMS réussi » et l'état passe à
   **Actif et vérifié**. En cas d'échec, le bandeau l'indique et le détail de
   l'erreur apparaît dans l'état du canal ; corriger la configuration puis
   recommencer.
5. Toute modification d'un paramètre du fournisseur invalide immédiatement la
   vérification : l'état repasse à **Inactif — non vérifié** et un nouveau test
   réussi est nécessaire avant tout envoi réel.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

La suite d'acceptation SMS peut être lancée seule :

```bash
python3 -m unittest tests.test_sms_acceptance -v
```

## Sécurité

- Toutes les valeurs dynamiques des pages HTML sont échappées.
- Toute requête `POST`, `PUT`, `PATCH` ou `DELETE` exige un jeton CSRF lié au
  cookie de session (`X-CSRF-Token` ou champ `csrf_token`).
- Les cookies de session sont `HttpOnly`, `SameSite=Lax`, et `Secure` lorsque
  `service.environment` vaut `production`.
- Les en-têtes CSP, `nosniff`, `X-Frame-Options` et `Referrer-Policy` sont
  ajoutés à chaque réponse.
- Les tentatives de livraison ne stockent qu'un destinataire masqué et une
  erreur expurgée et tronquée.
