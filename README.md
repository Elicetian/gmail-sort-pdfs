# mail-sort-pdfs — Terraform

Lambda AWS qui classe les PDFs reçus par Gmail vers Google Drive via Claude, sur un schedule journalier.

```
EventBridge Scheduler (cron 8h Paris)
    └─▶ Lambda mail-sort-pdfs (Python 3.12, 256 MB, 120s)
            ├─ SSM Parameter Store  → credentials Google + clé Anthropic
            ├─ Gmail API            → cherche les mails avec PDF
            ├─ Anthropic API        → classifie chaque PDF via Claude
            └─ Google Drive API     → upload + mise à la corbeille du mail
```

## Déploiement initial

### 1. Bucket Terraform + backend.hcl

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="mail-sort-pdfs-tfstate-${ACCOUNT_ID}"

aws s3api create-bucket --bucket "$BUCKET" --region eu-west-1 \
  --create-bucket-configuration LocationConstraint=eu-west-1
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

echo "bucket = \"$BUCKET\"" > backend.hcl
```

### 2. Paramètres SSM

```bash
REGION=eu-west-1

# Extraire depuis token.json (généré par sort_pdfs.py --auth)
aws ssm put-parameter --region $REGION --type SecureString \
  --name /mail-sort-pdfs/google/client_id \
  --value "$(jq -r .client_id ../token.json)"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /mail-sort-pdfs/google/client_secret \
  --value "$(jq -r .client_secret ../token.json)"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /mail-sort-pdfs/google/refresh_token \
  --value "$(jq -r .refresh_token ../token.json)"

aws ssm put-parameter --region $REGION --type SecureString \
  --name /mail-sort-pdfs/anthropic/api_key \
  --value "sk-ant-..."
```

### 3. Terraform

```bash
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

## Opérations courantes

### Tester manuellement

```bash
aws lambda invoke \
  --function-name mail-sort-pdfs \
  --region eu-west-1 \
  --log-type Tail \
  --query 'LogResult' --output text \
  /tmp/response.json | base64 -d
cat /tmp/response.json
```

### Suivre les logs en direct

```bash
aws logs tail /aws/lambda/mail-sort-pdfs --region eu-west-1 --follow
```

### Mettre à jour le code

Modifier `lambda/handler.py` puis :

```bash
terraform apply
```

### Rotation de la clé Anthropic

```bash
aws ssm put-parameter --region eu-west-1 --type SecureString \
  --name /mail-sort-pdfs/anthropic/api_key \
  --value "sk-ant-NOUVELLE_CLE" --overwrite
```
