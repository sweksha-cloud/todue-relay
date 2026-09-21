{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WriteOwnLogs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/todue-relay-pipeline:*"
    },
    {
      "Sid": "ReadThePipelineSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:todue-relay/pipeline-??????"
    }
  ]
}
