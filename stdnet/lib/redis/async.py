'''Asynchronous connector for redis-py.

Usage::

    from pulsar.apps.redis import RedisClient
    
    client = RedisClient('127.0.0.1:6349')
    pong = yield client.ping()
'''
from collections import deque
from itertools import chain
from functools import partial

import redis

import pulsar
from pulsar import is_async, multi_async, get_actor
from pulsar.utils.pep import ispy3k, map
from pulsar.apps import pubsub

from .extensions import get_script, RedisManager


class AsyncRedisRequest(object):
    
    def __init__(self, client, connection, timeout, encoding,
                  encoding_errors, command_name, args, reader,
                  raise_on_error=True, on_finished=None,
                  **options):
        self.client = client
        self.connection = connection
        self.timeout = timeout
        self.encoding = encoding
        self.encoding_errors = encoding_errors
        self.command_name = command_name.upper()
        self.reader = reader
        self.raise_on_error = raise_on_error
        self.on_finished = on_finished
        self.response = []
        self.last_response = False
        self.args = args
        pool = client.connection_pool
        if client.is_pipeline:
            self.command = pool.pack_pipeline(args)
        else:
            self.command = pool.pack_command(self.command_name, *args)
            args =(((command_name,), options),)
        self.args_options = deque(args)
        
    @property
    def key(self):
        return (self.connection, self.timeout)
    
    @property
    def address(self):
        return self.connection.address
    
    @property
    def is_pipeline(self):
        return not bool(self.command_name)
    
    def __repr__(self):
        if self.is_pipeline:
            return 'PIPELINE%s' % self.args
        else:
            return '%s%s' % (self.command_name, self.args)
    __str__ = __repr__
    
    def read_response(self):
        # For compatibility with redis-py
        return self.last_response
    
    def feed(self, data):
        self.reader.feed(data)
        while self.args_options:
            self.last_response = response = self.reader.gets()
            if response is False:
                break
            args, options = self.args_options.popleft()
            if not isinstance(response, Exception):
                response = self.client.parse_response(self, args[0], **options)
            self.response.append(response)
        if self.last_response is not False:
            return self.client.on_response(self.response, self.raise_on_error)
    
    
class RedisProtocol(pulsar.ProtocolConsumer):
    
    def __init__(self, connection=None):
        super(RedisProtocol, self).__init__(connection=connection)
        self.all_requests = []
        
    def chain_request(self, request):
        self.all_requests.append(request)
        
    def new_request(self, request=None):
        if request is None:
            self._requests = deque(self.all_requests)
            if self._requests:
                request = self._requests.popleft()
        return super(RedisProtocol, self).new_request(request)
    
    def start_request(self):
        self.transport.write(self.current_request.command)
    
    def data_received(self, data):
        response = self.current_request.feed(data)
        if response is not None:
            # The request has finished
            if self._requests:
                self.new_request(self._requests.popleft())
            elif self.current_request.on_finished:
                self.current_request.on_finished(self, response)
            else:
                self.finished(response)
                
    
class AsyncConnectionPool(pulsar.Client, RedisManager):
    '''A :class:`pulsar.Client` for managing a connection pool with redis
data-structure server.'''
    connection_pools = {}
    consumer_factory = RedisProtocol
    
    def __init__(self, address, db=0, password=None, encoding=None, reader=None,
                 encoding_errors='strict', **kwargs):
        super(AsyncConnectionPool, self).__init__(**kwargs)
        self.encoding = encoding or 'utf-8'
        self.encoding_errors = encoding_errors or 'strict'
        self.password = password
        self._setup(address, db, reader)
    
    def request(self, client, command_name, *args, **options):
        request = self._new_request(client, command_name, args, **options)
        return self.response(request)
    
    def request_pipeline(self, pipeline, raise_on_error=True):
        commands = pipeline.command_stack
        if not commands:
            return ()
        if pipeline.is_transaction:
            commands = list(chain([(('MULTI', ), {})], commands,
                                  [(('EXEC', ), {})]))
        request = self._new_request(pipeline, '', commands,
                                    raise_on_error=raise_on_error)
        return self.response(request)
        
    def response(self, request):
        connection = self.get_connection(request)
        consumer = self.consumer_factory(connection)
        # If this is a new connection we need to select database and login
        if not connection.processed:
            client = request.client
            if client.is_pipeline:
                client = client.client
            c = self.connection
            if self.password:
                req = self._new_request(client, 'auth', (self.password,))
                consumer.chain_request(req)
            if c.db:
                req = self._new_request(client, 'select', (c.db,))
                consumer.chain_request(req)
        consumer.chain_request(request)
        consumer.new_request()
        return consumer.on_finished
            
    def _new_request(self, client, command_name, args, **options):
        return AsyncRedisRequest(client, self.connection, self.timeout,
                                 self.encoding, self.encoding_errors,
                                 command_name, args, self.redis_reader(),
                                 **options)
        
    def execute_script(self, client, to_load, callback):
        # Override execute_script so that we execute after scripts have loaded
        if to_load:
            results = []
            for name in to_load:
                s = get_script(name)
                results.append(client.script_load(s.script))
            return multi_async(results).add_callback(callback)
        else:
            return callback()
        

class Subscriber(RedisProtocol):
    
    def __init__(self, reader, pubsub, connection=None):
        self.reader = reader
        self.pubsub = pubsub
        super(Subscriber, self).__init__(connection=connection)
        
    def data_received(self, data):
        self.reader.feed(data)
        response = self.reader.gets()
        if response is not False:
            self.on_response(response)
    
    def on_response(self, response):
        "Parse the response from a publish/subscribe command"
        command, channel = [r.decode('utf-8') for r in response[:2]]
        if command == 'message':
            if self.pubsub.channel == channel:
                self.pubsub.broadcast(response[2])
        
        
class PubSub(pubsub.PubSub):
    '''Implements pulsar pubsub using redis PubSub commands'''
    def __init__(self, channel, client):
        self.channel = channel
        self.redis = client
        self.subscribe()
        
    def subscribe(self):
        # Subscribe to redis. Don't release connection so that we can use the
        # publish command too.
        self.redis.execute_command('subscribe', self.channel,
                                   on_finished=self._subscribe)
     
    def publish(self, message):
        message = self.encode(message)
        return self.redis.publish(self.channel, message)
    
    def _subscribe(self, protocol, response):
        reader = protocol.current_request.reader
        self.connection = self.redis.connection_pool.upgrade(
                                        protocol.connection,
                                        partial(Subscriber, reader, self))