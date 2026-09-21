{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SchedulerServiceMayAssumeThisRole",
      "Effect": "Allow",
      "Principal": { "Service": "scheduler.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": { "StringEquals": { "aws:SourceAccount": "${ACCOUNT_ID}" } }
    }
  ]
}
