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
- journaux applicatifs, endpoint de santé et serveur de développement.

Le canal email est le canal obligatoire. L'intégration SMS est ajoutée par les
tâches suivantes du plan ; ce socle ne contacte aucun fournisseur et ne fait
aucun appel réseau au démarrage.

## Démarrage

```bash
cp config/app.example.json config/app.json   # puis adapter les valeurs
python3 -m app
```

Sans fichier `config/app.json`, le service démarre avec les valeurs par défaut
(serveur sur `127.0.0.1:8000`, base `var/alerts.sqlite3`) et un secret de
signature éphémère. Le fichier de configuration peut aussi être indiqué par
`--config` ou `ALERTS_CONFIG_FILE`.

- Page d'état : <http://127.0.0.1:8000/>
- Santé JSON : <http://127.0.0.1:8000/health> (alias `/healthz`)

Point d'entrée WSGI pour un serveur de production :

```bash
gunicorn wsgi:application
```

## Configuration

Le document JSON comporte les sections `service`, `server`, `database`,
`logging` et `email`. Les clés inconnues sont ignorées.

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

La section `email` est facultative. Si elle est renseignée, `smtp_host`,
`sender` et `recipient` doivent l'être ensemble. Le mot de passe n'est jamais
affiché par la configuration masquée ni journalisé.

## Tests

```bash
python3 -m unittest discover -s tests -v
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
