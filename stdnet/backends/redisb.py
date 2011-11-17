from collections import namedtuple

import stdnet
from stdnet.utils import iteritems, to_string, map
from stdnet.backends.structures import structredis
from stdnet.lib import redis, connection

MIN_FLOAT =-1.e99
EMPTY_DICT = {}

OBJ = 'obj'
UNI = 'uni'
IDX = 'idx'
pipeattr = lambda pipe,p,name : getattr(pipe,p+name)


redis_connection = namedtuple('redis_connection',
                              'host port db password socket_timeout decode')
 

class RedisTransaction(stdnet.Transaction):
    default_name = 'redis-transaction'
    
    def _execute(self):
        '''Commit cache objects to database.'''
        cursor = self.cursor
        for id,cachepipe in iteritems(self._cachepipes):
            el = getattr(self.server,cachepipe.method)(id)
            el._save_from_pipeline(cursor, cachepipe.pipe)
            cachepipe.pipe.clear()
                    
        if not self.emptypipe():
            return cursor.execute()
        
    def emptypipe(self):
        if hasattr(self.cursor,'execute'):
            return len(self.cursor.command_stack) <= 1
        else:
            return True


class add2set(object):

    def __init__(self, server, pipe, meta):
        self.server = server
        self.pipe = pipe
        self.meta = meta
    
    def __call__(self, key, id, score = None, obj = None, idsave = True):
        ordering = self.meta.ordering
        if ordering:
            if obj is not None:
                v = getattr(obj,ordering.name,None)
                score = MIN_FLOAT if v is None else ordering.field.scorefun(v)
            elif score is None:
                # A two way trip here.
                idset = self.meta.basekey('id')
                score = self.server.redispy.zscore(idset,id)
            if idsave:
                self.pipe.zadd(key, score, id)
        elif idsave:
            self.pipe.sadd(key, id)
        return score
        
        
class RedisQuery(stdnet.BeckendQuery):
    
    @property
    def simple(self):
        return isinstance(self.query_set,list)
    
    def _unique_set(self, name, values):
        '''Handle filtering over unique fields'''
        key = self.meta.tempkey()
        pipe = self.pipe
        add = self.add
        if name == 'id':
            for id in values:
                add(key,id)
        else:
            bkey = self.meta.basekey
            rpy = self.server.redispy
            for value in values:
                hkey = bkey(UNI,name)
                id = rpy.hget(hkey, value)
                add(key,id)
        pipe.expire(key,self.expire)
        return key
    
    def _query(self, qargs, setoper, key = None, extra = None):
        pipe = self.pipe
        meta = self.meta
        keys = []
        sha  = self._sha
        if qargs:
            for q in qargs:
                sha.write(q.__repr__().encode())
                values = q.values
                if q.unique:
                    if q.lookup == 'in':
                        keys.append(self._unique_set(q.name, values))
                    else:
                        raise ValueError('Not available')
                else:
                    if isinstance(values,self.query_class):
                        rqs = values.backend_query()
                        if rqs.simple:
                            values = rqs.query_set
                        else:
                            keys.append(rqs.query_set)
                            continue
                    if len(values) == 1:
                        keys.append(meta.basekey(IDX,q.name,values[0]))
                    else:
                        insersept = [meta.basekey(IDX,q.name,value)\
                                      for value in values]
                        tkey = self.meta.tempkey()
                        if q.lookup == 'in':
                            self.union(tkey,insersept).expire(tkey,self.expire)
                        #elif q.lookup == 'contains':
                        #    self.intersect(tkey,insersept).expire(tkey,self.expire)
                        else:
                            raise ValueError('Lookup {0} Not available'\
                                             .format(q.lookup))    
                        keys.append(tkey)
        
        if extra:
            for id in extra:
                sha.write(id.encode('utf-8'))
                keys.append(id)
        
        if keys:
            if key:
                keys.append(key)
            if len(keys) > 1:
                key = self.meta.tempkey()
                setoper(key, keys).expire(key,self.expire)
            else:
                key = keys[0]
                
        return key
        
    def zism(self, r):
        return r is not None
    
    def sism(self, r):
        return r
    
    def build_from_query(self, queries):
        '''Build a set of ids from an external query (a query on a
different model) which has a *field* containing current model ids.'''
        keys = []
        pipe = self.pipe
        sha = self._sha
        for q in queries:
            sha.write(q.__repr__().encode())
            query = q.query 
            qset = query.backend_query()
            qset.execute_query()
            qset = qset.query_set
            db = query._meta.cursor.redispy.db
            if db != pipe.db:
                raise ValueError('Indexes in a different database')
                # In a different redis database. We need to move the set
                query._meta.cursor.redispy.move(qset,pipe.db)
                pipe.expire(qset,self.expire)
                
            skey = self.meta.tempkey()
            okey = query._meta.basekey(OBJ,'*->{0}'.format(q.field))
            pipe.sort(qset, by = 'nosort', get = okey, storeset = skey)\
                    .expire(skey,self.expire)
            keys.append(skey)
        if len(keys) == 1:
            tkey = keys[0]
        else:
            tkey = self.meta.tempkey()
            self.intersect(tkey,keys).expire(tkey,self.expire)
        return tkey
    
    def build(self, fargs, eargs, queries):
        meta = self.meta
        server = self.server
        self.idset = idset = meta.basekey('id')
        p = 'z' if meta.ordering else 's'
        self.pipe = pipe = self.server.redispy.pipeline()
        if p == 'z':
            pismember =  pipeattr(pipe,'','zrank')
            self.ismember =  pipeattr(server.redispy,'','zrank')
            chk = self.zism
        else:
            pismember =  pipeattr(pipe,'','sismember')
            self.ismember =  pipeattr(server.redispy,'','sismember')
            chk = self.sism
        
        if self.qs.simple:
            allids = []
            for q in fargs:
                if q.name == 'id':
                    ids = q.values
                else:
                    key = meta.basekey(UNI,q.name)
                    ids = server.redispy.hmget(key, q.values)
                for id in ids:
                    if id is not None:
                        allids.append(id)
                        pismember(idset,id)
            self.query_set = [id for (id,r) in zip(allids,pipe.execute())\
                              if chk(r)]
        else:
            self.intersect = pipeattr(pipe,p,'interstore')
            self.union = pipeattr(pipe,p,'unionstore')
            self.diff = pipeattr(pipe,p,'diffstore')
            self.card = pipeattr(server.redispy,p,'card')
            self.add = add2set(server,pipe,meta)
            if queries:
                idset = self.build_from_query(queries)
            key1 = self._query(fargs,self.intersect,idset,self.qs.filter_sets)
            key2 = self._query(eargs,self.union)
            if key2:
                key = meta.tempkey()
                self.diff(key,(key1,key2)).expire(key,self.expire)
            else:
                key = key1
            self.query_set = key
            
    def execute_query(self):
        if not self.simple and self.sha:
            if self.timeout:
                key = self.meta.tempkey(sha)
                self.query_set = key
                if not self.server.redispy.exists(key):
                    self.pipe.rename(self.query_set,key)
                    return self.pipe.execute()
            else:
                return self.pipe.execute()
        
    def order(self):
        '''Perform ordering with respect model fields.'''
        if self.qs.ordering:
            sort_by = self.qs.ordering
            skey = self.meta.tempkey()
            okey = self.meta.basekey(OBJ,'*->{0}'.format(sort_by.name))
            pipe = self.server.redispy.pipeline()
            pipe.sort(self.query_set,
                      by = okey,
                      desc = sort_by.desc,
                      store = skey,
                      alpha = sort_by.field.internal_type == 'text')\
                .expire(skey,self.expire).execute()
            return skey
    
    def _count(self):
        if self.simple:
            return len(self.query_set)
        else:
            return self.card(self.query_set)
    
    def _has(self, val):
        if self.simple:
            return val in self.query_set
        else:
            return True if self.ismember(self.query_set, val) else False
    
    def get_redis_slice(self, slic):
        if slic:
            start = slic.start or 0
            stop = slic.stop or -1
            if stop > 0:
                stop -= 1
        else:
            start = 0
            stop = -1
        return start,stop
    
    def _items(self, slic):
        # Unwind the database query
        if self.simple:
            ids = self.query_set
            if slic:
                ids = ids[slic]
        else:
            skey = self.order()
            if skey:
                start,stop = self.get_redis_slice(slic)
                ids = self.server.redispy.lrange(skey,start,stop)
            elif self.meta.ordering:
                start,stop = self.get_redis_slice(slic)
                if self.meta.ordering.desc:
                    command = self.server.redispy.zrevrange
                else:
                    command = self.server.redispy.zrange
                ids = command(self.query_set,start,stop)
            else:
                ids = list(self.server.redispy.smembers(self.query_set))
                if slic:
                    ids = ids[slic]
        
        # Load data
        if ids:
            bkey = self.meta.basekey
            pipe = None
            fields = self.qs.fields or None
            fields_attributes = None
            if fields:
                fields, fields_attributes = self.meta.server_fields(fields)
                if fields:
                    pipe = self.server.redispy.pipeline()
                    hmget = pipe.hmget
                    for id in ids:
                        hmget(bkey(OBJ,to_string(id)),fields_attributes)
            else:
                pipe = self.server.redispy.pipeline()
                hgetall = pipe.hgetall
                for id in ids:
                    hgetall(bkey(OBJ,to_string(id)))
            if pipe is not None:
                result = self.server.make_objects(self.meta, ids,
                                            pipe.execute(), fields,
                                            fields_attributes)
            else:
                result = self.server.make_objects(self.meta, ids)
            return self.load_related(result)
        else:
            return ids
    

class BackendDataServer(stdnet.BackendDataServer):
    Transaction = RedisTransaction
    Query = RedisQuery
    structure_module = structredis
    connection_pools = {}
    _redis_clients = {}
    
    def __init__(self, name, server, db = 0,
                 password = None, socket_timeout = None,
                 decode = None, **params):
        super(BackendDataServer,self).__init__(name,**params)
        servs = server.split(':')
        host = servs[0] if servs[0] is not 'localhost' else '127.0.0.1'
        port = int(servs[1]) if len(servs) == 2 else 6379
        socket_timeout = int(socket_timeout) if socket_timeout else None
        cp = redis_connection(host, port, db, password, socket_timeout, decode)
        if cp in self.connection_pools:
            connection_pool = self.connection_pools[cp]
        else:
            connection_pool = redis.ConnectionPool(**cp._asdict())
            self.connection_pools[cp] = connection_pool 
        self.redispy = redis.Redis(connection_pool = connection_pool)
        self.execute_command = self.redispy.execute_command
        self.incr            = self.redispy.incr
        self.clear           = self.redispy.flushdb
        self.delete          = self.redispy.delete
        self.keys            = self.redispy.keys
    
    def cursor(self, pipelined = False):
        return self.redispy.pipeline() if pipelined else self.redispy
    
    def issame(self, other):
        return self.redispy == other.redispy
        
    def disconnect(self):
        self.redispy.connection_pool.disconnect()
    
    def unwind_query(self, meta, qset):
        '''Unwind queryset'''
        table = meta.table()
        ids = list(qset)
        make_object = self.make_object
        for id,data in zip(ids,table.mget(ids)):
            yield make_object(meta,id,data)
    
    def set_timeout(self, id, timeout):
        if timeout:
            self.execute_command('EXPIRE', id, timeout)
    
    def has_key(self, id):
        return self.execute_command('EXISTS', id)
    
    def _set(self, id, value, timeout):
        if timeout:
            return self.execute_command('SETEX', id, timeout, value)
        else:
            return self.execute_command('SET', id, value)
    
    def _get(self, id):
        return self.execute_command('GET', id)
    
    def _loadfields(self, obj, toload):
        if toload:
            fields = self.redispy.hmget(obj._meta.basekey(OBJ,obj.id), toload)
            return dict(zip(toload,fields))
        else:
            return EMPTY_DICT

    def _save_object(self, obj, newid, transaction):        
        # Add object data to the model hash table
        pipe = transaction.cursor
        obid = obj.id
        meta = obj._meta
        bkey = meta.basekey
        data = obj.cleaned_data
        indices = obj.indices
        if data:
            pipe.hmset(bkey(OBJ,obid),data)
        #hash.addnx(objid, data)
        
        if newid or indices:
            add = add2set(self,pipe,meta)
            score = add(bkey('id'), obid, obj=obj, idsave=newid)
            fields = self._loadfields(obj,obj.toload)
            
        if indices:
            rem = pipeattr(pipe,'z' if meta.ordering else 's','rem')
            if not newid:
                pipe.delpattern(meta.tempkey('*'))
            
            # Create indexes
            for field,value,oldvalue in indices:
                name = field.name
                if field.unique:
                    name = bkey(UNI,name)
                    if not newid:
                        oldvalue = fields.get(field.name,oldvalue)
                        pipe.hdel(name, oldvalue)
                    pipe.hset(name, value, obid)
                else:
                    if not newid:
                        oldvalue = fields.get(field.name,oldvalue)
                        rem(bkey(IDX,name,oldvalue), obid)
                    add(bkey(IDX,name,value), obid, score = score)
                        
        return obj
    
    def _delete_object(self, obj, transaction):
        dbdata = obj._dbdata
        id = dbdata['id']
        # Check for multifields and remove them
        meta = obj._meta
        bkey = meta.basekey
        pipe = transaction.cursor
        rem = pipeattr(pipe,'z' if meta.ordering else 's','rem')
        #remove the hash table
        pipe.delete(meta.basekey(OBJ,id))
        #remove the id from set
        rem(bkey('id'), id)
        # Remove multifields

        fids = obj._meta.multifields_ids_todelete(obj)
        if fids:
            transaction.cursor.delete(*fids)
        # Remove indices
        if meta.indices:
            rem = pipeattr(pipe,'z' if meta.ordering else 's','rem')
            toload = []
            for field in meta.indices:
                name = field.name
                if name not in dbdata:
                    toload.append(name)
                else:
                    if field.unique:
                        pipe.hdel(bkey(UNI,name), dbdata[name])
                    else:
                        rem(bkey(IDX,name,dbdata[name]), id)
            fields = self._loadfields(obj,toload)
            for name,value in iteritems(fields):
                field = meta.dfields[name]
                if field.unique:
                    pipe.hdel(bkey(UNI,name), value)
                else:
                    rem(bkey(IDX,name,value), id)
    
    def flush(self, meta):
        '''Flush all model keys from the database'''
        # The scripting delete
        pattern = '{0}*'.format(meta.basekey())
        return self.redispy.delpattern(pattern)
            
    def instance_keys(self, obj):
        meta = obj._meta
        keys = [meta.basekey(OBJ,obj.id)]
        for field in meta.multifields:
            f = getattr(obj,field.attname)
            keys.append(f.id)
        return keys
