# "ticketId","threadId","emailMetaId"
# 185454,"19416148974ffa95",78967
# 185246,"193e287957e63f5a",78819
# 185070,"193d2ea3053694f1",76580
# 185047,"193ce874d73acaa6",78077
# 185036,"193ce10f5f8414ab",78375

import boto3
from summarizer.config.settings import Settings
import json

sqs = boto3.client('sqs')
s = Settings()

url = s.sqs.queue_url

data = json.dumps({
    "ticketId": 185036,
    "emailMetaId": 78375,
    "threadId": "193ce10f5f8414ab",
})

sqs.send_message(
    QueueUrl=url,
    MessageBody=data
)



