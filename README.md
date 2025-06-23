# localpost

[![PyPI package version](https://img.shields.io/pypi/v/localpost)](https://pypi.org/project/localpost/)
![Python versions](https://img.shields.io/pypi/pyversions/localpost)
<br>
[![Code coverage](https://img.shields.io/sonar/coverage/alexeyshockov_localpost.py?server=https%3A%2F%2Fsonarcloud.io)](https://sonarcloud.io/project/overview?id=alexeyshockov_localpost.py)

Simple in-process task scheduler & consumers framework for different message brokers.

## Scheduler

TBD

### Tasks

TBD, including:
- can accept 0 or 1 argument (trigger's value)
- return values will be available in a stream

#### Concurrency

Solely depends on the trigger.

### Triggers & decorators

TBD

### Built-in triggers & decorators

TBD, including:
- every()
- delay()
- skip_first()

### Custom triggers & decorators

TBD

## Consumers

TBD, including basic Kafka & SQS examples.

## Flow & flow ops

TBD

### Handlers & handler managers

TBD

### Building pipelines

TBD, including:
- decorator style
- `<<` operator

### Built-in ops

TBD, including:
- map, filter, flatmap
- buffer
- batch

### Custom middlewares

TBD

## Hosting

TBD

### Hosted services

TBD, including:
- combining multiple service together
  - run services in parallel
  - wrap service(s) with another one

### Running multiple services

TBD, including:
- combining multiple services, using host's `+` operator
- wrapping a service (or a set of services) with another one, using host's `>>` operator

### AppHost

TBD

## Motivation

TBD, including:
- type safety
- FastAPI-like
  - decorators to create scheduled tasks & hosted services
  - middlewares
- Async first
  - AnyIO backed (mainly for structured concurrency, compatibility with Trio as a bonus)
