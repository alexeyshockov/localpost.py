"""
Kafka Protobuf deserializer.

Mainly to show the approach on how to create a custom deserializer. Not intended to be a generic solution.
"""

from __future__ import annotations

import warnings
from typing import Callable, ParamSpec, TypeAlias, TypeVar

from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from google.protobuf.message import Message as PbMessage

from localpost.consumers.kafka import KafkaMessage

T = TypeVar("T", bound=PbMessage)
P = ParamSpec("P")

__all__ = [
    "protobuf_deserializer_for",
]

Deserializer: TypeAlias = Callable[[KafkaMessage | bytes], T]


# See also https://github.com/confluentinc/confluent-kafka-python/blob/master/examples/protobuf_consumer.py
def protobuf_deserializer_for(message_type: type[T]) -> Deserializer[T]:
    # Confluent SDK uses some deprecated Protobuf methods, just skip it
    # (Already fixed in confluent-kafka 2.6+?..)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # "MessageFactory class is deprecated. Please use GetMessageClass() instead of MessageFactory.GetPrototype."
        deserializer = ProtobufDeserializer(
            message_type,  # type: ignore[arg-type]
            {"use.deprecated.format": False},
        )

    def deserialize(m: KafkaMessage | bytes) -> T:
        if isinstance(m, KafkaMessage):
            context = SerializationContext(m.payload.topic(), MessageField.VALUE)
            return deserializer(m.value, context)  # type: ignore[return-value]
        return deserializer(m)  # type: ignore[return-value]

    return deserialize
