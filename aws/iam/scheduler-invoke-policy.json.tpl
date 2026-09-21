{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeOnlyThePipelineFunction",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": [
        "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:todue-relay-pipeline",
        "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:todue-relay-pipeline:*"
      ]
    }
  ]
}
