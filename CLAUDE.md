# mail-sort-pdfs — CLAUDE.md

Lambda AWS qui classe les PDFs reçus par Gmail vers Google Drive via Claude (claude-sonnet-4-6), déclenchée chaque matin à 8h heure Paris.

---

## Structure

```
sort-pdf/
├── main.tf                  # Toute l'infra (Lambda, IAM, Scheduler, Alarm)
├── variables.tf             # region
├── outputs.tf               # lambda_arn, lambda_name
├── terraform.tfvars         # Gitignored — bucket S3 du tfstate
├── terraform.tfvars.example # Template à copier
├── backend.hcl              # Gitignored — bucket S3 du tfstate
├── .gitignore
├── README.md
├── CLAUDE.md
└── lambda/
    ├── handler.py           # Code Lambda
    └── requirements.txt     # Dépendances Python
```

---

## Infrastructure (main.tf)

| Ressource | Valeur |
|---|---|
| Lambda | `mail-sort-pdfs`, Python 3.12, 256 MB, 120s |
| Schedule | `cron(0 8 * * ? *)` timezone `Europe/Paris` |
| Tag | `PRODUCT = "mail-sort-pdfs"` sur toutes les ressources |
| State backend | S3 `mail-sort-pdfs-tfstate-<account-id>` (configuré dans `backend.hcl`) |

Le `null_resource.pip_install` installe les dépendances via `uv pip install` avec les flags `--python-version 3.12 --python-platform x86_64-unknown-linux-gnu` (wheels Linux compatibles Lambda) avant de zipper `lambda_build/`.

---

## Paramètres SSM (`/mail-sort-pdfs/`)

Lus en un seul appel `get_parameters` au démarrage de la Lambda.

| Paramètre | Type | Contenu |
|---|---|---|
| `/mail-sort-pdfs/google/client_id` | SecureString | OAuth2 client ID (depuis credentials.json) |
| `/mail-sort-pdfs/google/client_secret` | SecureString | OAuth2 client secret |
| `/mail-sort-pdfs/google/refresh_token` | SecureString | Refresh token Google (depuis token.json) |
| `/mail-sort-pdfs/anthropic/api_key` | SecureString | Clé API Anthropic |
| `/mail-sort-pdfs/drive_folders` | String | JSON map clé → ID dossier Google Drive |

Pas de write-back SSM : le refresh_token ne change jamais, l'access_token est regénéré en mémoire à chaque invocation via `Credentials(token=None, refresh_token=..., ...)`.

L'IAM Lambda n'a que `ssm:GetParameter` + `ssm:GetParameters` — pas de `PutParameter`.

---

## handler.py — logique

1. Charge les 5 params SSM en un seul appel
2. Construit les credentials Google en mémoire (pas de fichier token)
3. Cherche dans Gmail : `has:attachment filename:pdf label:INBOX -label:pdf-a-classer`
4. Déduplique par header `Message-ID`
5. Pour chaque PDF : extrait le texte avec pypdf (~800 tokens), fallback PDF image si scanné
6. Envoie à Claude avec métadonnées (expéditeur, sujet, date, nom de fichier)
7. Décision par mail :
   - Tous confiants ≥ 80% → upload Drive + corbeille
   - Au moins un incertain → label Gmail `pdf-a-classer`
   - Tous `AMBIGUOUS` → ignoré, rien modifié

### Clés de classification et conventions de nommage

| Clé | Convention |
|---|---|
| `factures` | `YYYY-MM-DD-description-courte.pdf` |
| `parking` | `YYYY-MM_avis-echeance-parking.pdf` |
| `appt_gestion` | `YYYY-MM_appel-fonds-appt.pdf` |
| `copro_appel_fonds` | `YYYY-MM_appel-fonds-copro.pdf` |
| `copro_gestion` | `YYYY-MM_compte-rendu-gestion.pdf` |
| `copro_ag` | `YYYY-MM-DD_ag-copro.pdf` |
| `appt_ag` | `YYYY-MM-DD_ag-appt.pdf` |
| `appt_travaux` | `YYYY-MM-DD-description-appt.pdf` |
| `copro_travaux` | `YYYY-MM-DD-description-copro.pdf` |
| `appt_conseil_syndical` | `YYYY-MM-DD-description-cs-appt.pdf` |
| `copro_conseil_syndical` | `YYYY-MM-DD-description-cs-copro.pdf` |
| `appt_tenant` | `YYYY-MM-DD-description-tenant.pdf` |
| `appt_fiscal` | `YYYY_aide-declaration-revenus-fonciers.pdf` |

---

## Points de vigilance

- **Wheels Lambda** : toujours builder avec `--python-platform x86_64-unknown-linux-gnu` — sans ça, les extensions C (pydantic_core) ne chargent pas sur Amazon Linux
- **Rebuild forcé** : si le `null_resource` ne se redéclenche pas après un changement de commande, faire `terraform taint null_resource.pip_install`
- **Google OAuth** : le projet GCP s'appelle "API project" — c'est normal, nom hérité d'un ancien projet
- **Scopes OAuth** : `gmail.modify` + `drive.file` — `drive.file` ne voit que les fichiers créés par l'app (suffisant pour la Lambda)
