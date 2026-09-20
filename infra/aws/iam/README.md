# IAM policy documents for the pipeline Lambda

| File | What it is |
|---|---|
| `lambda-trust-policy.json` | The role's **trust policy**: only the Lambda service may assume it. |
| `scheduler-trust-policy.json.tpl` | The **scheduler role's** trust policy: only EventBridge Scheduler in this account may assume it (`aws:SourceAccount` condition). |
| `scheduler-invoke-policy.json.tpl` | The scheduler role's permission: `lambda:InvokeFunction` on the pipeline function and nothing else. |
| `lambda-execution-policy.json.tpl` | The role's **permissions**, a template. `${ACCOUNT_ID}` and `${REGION}` are filled in when applied, so the account ID isn't committed. |

Two roles, two directions. The **execution role** is what the function may do; the
**scheduler role** is what lets the schedule start the function. The execution role may
do exactly two things: write to its own log group, and read
one secret. Nothing else: no S3, no wildcard resources.

A Secrets Manager ARN ends in a hyphen plus six random characters
(`...:secret:todue-relay/pipeline-AbC123`), so the policy names the secret with
`-??????` (IAM's single-character wildcard) instead of `*`. That lets the role be
created before the secret exists, while still matching only that one secret.

Render and apply (from the repo root, signed in with the `todue` profile):

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-2
sed "s/\${ACCOUNT_ID}/$ACCOUNT_ID/g; s/\${REGION}/$REGION/g" \
  infra/aws/iam/lambda-execution-policy.json.tpl > /tmp/lambda-execution-policy.json

aws iam create-role --role-name todue-relay-pipeline-role \
  --assume-role-policy-document file://infra/aws/iam/lambda-trust-policy.json
aws iam put-role-policy --role-name todue-relay-pipeline-role \
  --policy-name pipeline-permissions --policy-document file:///tmp/lambda-execution-policy.json
```

Both documents can be checked for free with IAM Access Analyzer's policy validator
before applying (`aws accessanalyzer validate-policy`).
