"""
  - create a Redis client connection,
  - write a key like demo:greeting with value hello world,
  - read the same key back,
  - print the value so you can verify round-trip.
"""

import redis

r = redis.Redis(host='localhost', port=6379, decode_responses=True)
r.set('demo:greeting', 'hello world')

greeting = r.get('demo:greeting')
print(greeting)

r.close()