from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypedDict

try:
    from types_boto3_sqs import SQSClient as BotoSqsClient
except ImportError:
    # BotoSqsClient: TypeAlias = Any
    # Same trick as in types_boto3_sqs stubs
    from typing import Any as BotoSqsClient  # type: ignore[assignment]  # noqa

try:
    from types_boto3_sqs.literals import MessageSystemAttributeNameType
    from types_boto3_sqs.type_defs import (
        MessageAttributeValueOutputTypeDef,
        MessageTypeDef,
        ReceiveMessageRequestTypeDef,
    )
except ImportError:
    # Same trick as in types_boto3_sqs stubs
    from typing import Any as MessageSystemAttributeNameType  # type: ignore[assignment]  # noqa
    from typing import Any as MessageAttributeValueOutputTypeDef  # type: ignore[assignment]  # noqa
    from typing import Any as MessageTypeDef  # type: ignore[assignment]  # noqa
    from typing import Any as ReceiveMessageRequestTypeDef  # type: ignore[assignment]  # noqa


class LambdaEventRecordMessageAttributeValue(TypedDict):
    dataType: str
    stringValue: str
    binaryValue: bytes
    stringListValues: Sequence[str]
    binaryListValues: Sequence[bytes]


class LambdaEventRecord(TypedDict):
    """
    One record in the Lambda event.

    Example:
    {
        "messageId": "059f36b4-87a3-44ab-83d2-661975830a7d",
        "receiptHandle": "AQEBwJnKyrHigUMZj6rYigCgxlaS3SLy0a...",
        "body": "Test message.",
        "attributes": {
            "ApproximateReceiveCount": "1",
            "SentTimestamp": "1545082649183",
            "SenderId": "AIDAIENQZJOLO23YVJ4VO",
            "ApproximateFirstReceiveTimestamp": "1545082649185"
        },
        "messageAttributes": {
            "myAttribute": {
                "stringValue": "myValue",
                "stringListValues": [],
                "binaryListValues": [],
                "dataType": "String"
            }
        },
        "md5OfBody": "e4e68fb7bd0e697a0ae8f1bb342846b3",
        "eventSource": "aws:sqs",
        "eventSourceARN": "arn:aws:sqs:us-east-2:123456789012:my-queue",
        "awsRegion": "us-east-2"
    }
    """

    messageId: str
    receiptHandle: str
    body: str
    attributes: Mapping[MessageSystemAttributeNameType, str]
    messageAttributes: Mapping[str, LambdaEventRecordMessageAttributeValue]
    md5OfBody: str
    eventSource: str
    eventSourceARN: str
    awsRegion: str


class LambdaEvent(TypedDict):
    Records: Sequence[LambdaEventRecord]
