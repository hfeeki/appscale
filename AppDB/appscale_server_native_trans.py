#!/usr/bin/python
#
# Author: 
# Navraj Chohan (nchohan@cs.ucsb.edu)
# Soo Hwan Park (suwanny@gmail.com)
# Sydney Pang (pang@cs.ucsb.edu)
# See LICENSE file
import tornado.httpserver
import tornado.ioloop
import tornado.web

import sys
import socket
import os 
import types
import appscale_datastore
#import helper_functions
import SOAPpy
from dbconstants import *
import appscale_logger
import md5 
import random
import getopt
import threading
import datetime 
from google.appengine.api import api_base_pb
from google.appengine.api import datastore
from google.appengine.api import datastore_errors
from google.appengine.api import datastore_types
from google.appengine.api import users
from google.appengine.datastore import datastore_pb
from google.appengine.datastore import datastore_index
from google.appengine.datastore import datastore_stub_util
from google.appengine.runtime import apiproxy_errors
from google.net.proto import ProtocolBuffer
from google.appengine.datastore import entity_pb
from google.appengine.ext.remote_api import remote_api_pb
from SocketServer import BaseServer
from M2Crypto import SSL
from drop_privileges import *
from zkappscale import zktransaction

import time

DEBUG = False 
APP_TABLE = APPS_TABLE
USER_TABLE = USERS_TABLE
DEFAULT_USER_LOCATION = ".flatfile_users"
DEFAULT_APP_LOCATION = ".flatfile_apps"
HYPERTABLE_XML_TAG = "Name"
DEFAULT_DATASTORE = "files"
DEFAULT_SSL_PORT = 8443
DEFAULT_PORT = 4080
DEFAULT_ENCRYPTION = 1
CERT_LOCATION = "/etc/appscale/certs/mycert.pem"
KEY_LOCATION = "/etc/appscale/certs/mykey.pem"
SECRET_LOCATION = "/etc/appscale/secret.key"
VALID_DATASTORES = []
ERROR_CODES = []
app_datastore = []
logOn = False
logFilePtr = ""
zoo_keeper = ""

getKeyFromServer = False
soapServer = "localhost"
tableServer = ""
keyPort = 4343
keySecret = ""
KEYBLOCKSIZE = "50"
keyDictionaryLock = None
keyDictionary = {}

optimizedQuery = False
ID_KEY_LENGTH = 64
tableHashTable = {}

local_server_address = ""
HandlerClass = ""
ssl_cert_file = ""
ssl_key_file  = ""

DELETED = "DELETED___"
""" 
Deleted keys are DELETED/<row_key>
"""

""" 
keys for tables take the format
appname/Grandparent:<ID>/Parent:<ID>/Child:<ID>
for the entity table
"""
_PROPERTY_TYPE_TAGS = {
  datastore_types.Blob: entity_pb.PropertyValue.kstringValue,
  bool: entity_pb.PropertyValue.kbooleanValue,
  datastore_types.Category: entity_pb.PropertyValue.kstringValue,
  datetime.datetime: entity_pb.PropertyValue.kint64Value,
  datastore_types.Email: entity_pb.PropertyValue.kstringValue,
  float: entity_pb.PropertyValue.kdoubleValue,
  datastore_types.GeoPt: entity_pb.PropertyValue.kPointValueGroup,
  datastore_types.IM: entity_pb.PropertyValue.kstringValue,
  int: entity_pb.PropertyValue.kint64Value,
  datastore_types.Key: entity_pb.PropertyValue.kReferenceValueGroup,
  datastore_types.Link: entity_pb.PropertyValue.kstringValue,
  long: entity_pb.PropertyValue.kint64Value,
  datastore_types.PhoneNumber: entity_pb.PropertyValue.kstringValue,
  datastore_types.PostalAddress: entity_pb.PropertyValue.kstringValue,
  datastore_types.Rating: entity_pb.PropertyValue.kint64Value,
  str: entity_pb.PropertyValue.kstringValue,
  datastore_types.ByteString: entity_pb.PropertyValue.kstringValue,
  datastore_types.BlobKey: entity_pb.PropertyValue.kstringValue,
  datastore_types.Text: entity_pb.PropertyValue.kstringValue,
  type(None): 0,
  unicode: entity_pb.PropertyValue.kstringValue,
  users.User: entity_pb.PropertyValue.kUserValueGroup,
  }


class ThreadLogger:
  def __init__(self, log):
    self.logger_ = log
    self.log_lock = threading.Lock()

  def debug(self, string):
    return
    self.log_lock.acquire()
    print string
    self.logger_.info(string)
    self.log_lock.release()

logger = appscale_logger.getLogger("pb_server")


def getTableName(app_id, kind, namespace):
  return app_id + "___" + kind + "___" + namespace

def getRowKey(app_id, ancestor_list):
  if ancestor_list == None:
    logger.debug("Generate row key received null ancestor list")
    return ""

  key = app_id 

  # Note: mysql cannot have \ as the first char in the row key
  for a in ancestor_list:
    key += "/" 
    if a.has_type():
      key += a.type()

    if a.has_id():
      zero_padded_id = ("0" * (ID_KEY_LENGTH - len(str(a.id())))) + str(a.id())
      key += ":" + zero_padded_id
    elif a.has_name():
      # append _ if the name is a number, prevents collisions of key names
      if a.name().isdigit():
        key += ":__key__" + a.name()
      else: 
        key += ":" + a.name()
  return key


def getRootKey(app_id, ancestor_list):
  key = app_id # mysql cannot have \ as the first char in the row key
  a = ancestor_list[0]
  key += "/"

  # append _ if the name is a number, prevents collisions of key names
  if a.has_type():
    key += a.type()
  else:
    return None

  if a.has_id():
    zero_padded_id = ("0" * (ID_KEY_LENGTH - len(str(a.id())))) + str(a.id())
    key += ":" + zero_padded_id
  elif a.has_name():
    if a.name().isdigit():
      key += ":__key__" + a.name()
    else: 
      key += ":" + a.name()
  else:
    return None

  return key


def getRootKeyFromKeyType(app_id, key):
  ancestor_list = key._Key__reference.path().element_list()
  return getRootKey(app_id, ancestor_list)


def getRowKeyFromKeyType(app_id, key):
  ancestor_list = key._Key__reference.path().element_list()
  return getRowKey(app_id, ancestor_list)

def generate_unique_id(app_id, root, isChild):
  global keyDictionary
  global keyDictionaryLock

  if isChild:
    if not root:
      return -1

  index = None
  if isChild:
    index = app_id + "/" + str(root)
  else:
    index = app_id

  keyDictionaryLock.acquire()
  try:
    keyStart, keyEnd = keyDictionary[index]
  except:
    keyStart = 0
    keyEnd = 0

  key = 0
  if keyStart != keyEnd:
    key = keyStart
    keyStart = keyStart + 1
    keyDictionary[index]= keyStart, keyEnd
    keyDictionaryLock.release()
    return key
  else:
    try:
      if not isChild:
        keyStart, blockSize = zoo_keeper.generateIDBlock(app_id)
        keyStart = long(keyStart)
      else:
        keyStart, blockSize = zoo_keeper.generateIDBlock(app_id, root)
        keyStart = long(keyStart)
    except:
       print "="*60
       print "Exception: when getting id block"
       print "="*60
       keyDictionaryLock.release()
       return -1
  keyEnd = keyStart + long(blockSize)
  key = keyStart
  keyStart = keyStart + 1
  keyDictionary[index] = keyStart, keyEnd
  keyDictionaryLock.release()
  return key

  
def getRootKeyFromRef(app_id, ref):
  if not ref.has_path():
    return False
  path = ref.path()
  element_list = path.element_list()
  return  getRootKey(app_id, element_list)


def rollback_function(app_id, trans_id, root_key, change_set):
  pass




class MainHandler(tornado.web.RequestHandler):
  """
  Defines what to do when the webserver receives different types of 
  HTTP requests.
  """
  @tornado.web.asynchronous
  def get(self):
    self.write("Hi")
    self.finish() 
  # remote api request
  # sends back a response 
  def remote_request(self, app_id, http_request_data):
    apirequest = remote_api_pb.Request()
    apirequest.ParseFromString(http_request_data)
    apiresponse = remote_api_pb.Response()
    response = None
    errcode = 0
    errdetail = ""
    apperror_pb = None

    if not apirequest.has_method(): 
      errcode = datastore_pb.Error.BAD_REQUEST
      errdetail = "Method was not set in request"
      apirequest.set_method("NOT_FOUND")
    if not apirequest.has_request():
      errcode = datastore_pb.Error.BAD_REQUEST
      errdetail = "Request missing in call"
      apirequest.set_method("NOT_FOUND")
      apirequest.clear_request()
    method = apirequest.method()
    http_request_data = apirequest.request()

    #print "REQUEST:",method," AT time",time.time()
    if method == "Put":
      response, errcode, errdetail = self.put_request(app_id, 
                                                 http_request_data)
    elif method == "Get":
      response, errcode, errdetail = self.get_request(app_id, 
                                                 http_request_data)
    elif method == "Delete": 
      response, errcode, errdetail = self.delete_request(app_id, 
                                                    http_request_data)
    elif method == "RunQuery":
      response, errcode, errdetail = self.run_query(app_id, 
                                          http_request_data)
    elif method == "BeginTransaction":
      response, errcode, errdetail = self.begin_transaction_request(app_id, 
                                                      http_request_data)
    elif method == "Commit":
      response, errcode, errdetail = self.commit_transaction_request(app_id, 
                                                      http_request_data)
    elif method == "Rollback":
      response, errcode, errdetail = self.rollback_transaction_request(app_id, 
                                                        http_request_data)
    elif method == "AllocateIds":
      response, errcode, errdetail = self.allocate_ids_request(app_id, 
                                                http_request_data)
    elif method == "CreateIndex":
      errcode = datastore_pb.Error.PERMISSION_DENIED
      errdetail = "Create Index is not implemented" 
      logger.debug(errdetail)
      """
      response, errcode, errdetail = self.create_index_request(app_id, 
                                                appscale_version,
                                                http_request_data)
      """
    elif method == "GetIndices":
      errcode = datastore_pb.Error.PERMISSION_DENIED 
      errdetail = "GetIndices is not implemented" 
      logger.debug(errdetail)
      """
      response, errcode, errdetail = self.get_indices_request(app_id, 
                                               appscale_version,
                                               http_request_data)
      """
    elif method == "UpdateIndex":
      errcode = datastore_pb.Error.PERMISSION_DENIED 
      errdetail = "UpdateIndex is not implemented" 
      logger.debug(errdetail)
      """
      response, errcode, errdetail = self.update_index_request(app_id, 
                                                appscale_version,
                                                http_request_data)
      """
    elif method == "DeleteIndex":
      errcode = datastore_pb.Error.PERMISSION_DENIED 
      errdetail = "DeleteIndex is not implemented" 
      logger.debug(errdetail)

      """
      response, errcode, errdetail = self.delete_index_request(app_id, 
                                                appscale_version,
                                                http_request_data)
      """
    else:
      errcode = datastore_pb.Error.BAD_REQUEST 
      errdetail = "Unknown datastore message" 
      logger.debug(errdetail)
    
      
    apiresponse.set_response(response)
    if errcode != 0:
      apperror_pb = apiresponse.mutable_application_error()
      apperror_pb.set_code(errcode)
      apperror_pb.set_detail(errdetail)
    if errcode != 0:
      print "REPLY",method," AT TIME",time.time()
      print "errcode:",errcode
      print "errdetail:",errdetail
    self.write(apiresponse.Encode() )    


  def _getGlobalStat(self):
    global_stat_entity=datastore.Entity("__Stat_Total__", id=1)
    global_stat_entity[u"bytes"] = 0 #dummy value
    global_stat_entity[u"count"] = 0 #dummy value
    global_stat_entity[u"timestamp"] = 0 #dummy value
    return global_stat_entity


  def _getKinds(self,app_id):
    """ This is for the bulk uploaded
    """
    global app_datastore
    columns = ["classes"]
    kinds = app_datastore.get_entity(APP_TABLE, app_id, columns)
    #The first item : kinds[0] is always 'DB_ERROR:', so ignoring it
    kind_list = unicode(kinds[1]).split(":")
    #a dictionary to store unique kinds only
    #key : kind name, value : entity representing the kind
    kinds = {}
    kind_id=1 #assigning ids starting from 1
    for kind_name in kind_list:
      if kind_name.endswith("___"):
        kind_name=kind_name[:len(kind_name)-3]
      if not kind_name in kinds:
        kind_entity = datastore.Entity("__Stat_Kind__", id=kind_id)
        kind_entity[u"kind_name"] = unicode(kind_name)
        kinds[kind_name]=kind_entity
        kind_id=kind_id+1
    #return entities representing the kinds
    return kinds.values()

  def _getKindless(self,app_id):
    """ This is for kindless queries which are based on keys and 
        ancestors.
    """
    global app_datastore
    columns = ["classes"]
    kinds = app_datastore.get_entity(APP_TABLE, app_id, columns)
    #The first item : kinds[0] is always 'DB_ERROR:', so ignoring it
    kind_list = unicode(kinds[1]).split(":")
    kinds = []
    for kind_name in kind_list:
      if kind_name.endswith("___"):
        kind_name=kind_name[:len(kind_name)-3]
      kinds.append(kind_name)
    #return entities representing the kinds
    # remove dups
    kinds = list(set(kinds))
    return kinds

  def run_query(self, app_id, http_request_data):
    query = datastore_pb.Query(http_request_data)
    namespace = query.name_space()
    #logger.info("QUERY:%s" % query)
    results = []
    kinds = []                      

    if not query.has_kind():
      kinds = self._getKindless(app_id)
    else:
      kinds.append(query.kind())

    #Handling stat query for bulk download/upload
    if (str(kinds[0]) == "__Stat_Kind__"):
      results = self._getKinds(app_id)

    #Handling stat query for bulk download/upload      
    elif (str(kinds[0]) == "__Stat_Total__"):
      global_stat_entity=self._getGlobalStat()
      results = [global_stat_entity]

    #Handling other queries
    else:
      global app_datastore
      results = []
      for kind in kinds:
        # Fetch query from the datastore # 
        table_name = getTableName(app_id, kind, namespace)
        r = []
        if query.has_transaction():
          txn = query.transaction()
          r = app_datastore.get_table( table_name, ENTITY_TABLE_SCHEMA, txn.handle())   
        else: 
          r = app_datastore.get_table( table_name, ENTITY_TABLE_SCHEMA)
        err = r[0]
        if err not in ERROR_CODES:
          return (api_base_pb.VoidProto().Encode(),
                datastore_pb.Error.INTERNAL_ERROR,
                "Error running query--." + err)

        if len(r) > 1:
          res = r[1:]
        else:
          res = []

        # odds are versions
        versions = res[1::2]
        # evens are encoded entities
        res = res[0::2]
        if len(versions) != len(res):
          return(api_base_pb.VoidProto().Encode(),
                 datastore_pb.Error.INTERNAL_ERROR,
                 'The query had a bad number of results.')
        results.extend(res)
      for index, res in enumerate(results):
        results[index] = entity_pb.EntityProto(res) 
        results[index] = datastore.Entity._FromPb(results[index]) 

      # convert to objects
      (filters, orders) = datastore_index.Normalize(query.filter_list(),
                                                    query.order_list())
      _MAX_QUERY_COMPONENTS = 100

      datastore_stub_util.ValidateQuery(query, filters, orders,
          _MAX_QUERY_COMPONENTS)
      datastore_stub_util.FillUsersInQuery(filters)

      #logger.info("====results pre filter====")
      #logger.info("%s" % str(results))

      if query.has_ancestor():
        ancestor_path = query.ancestor().path().element_list()
        def is_descendant(entity):
          path = entity.key()._Key__reference.path().element_list()
          return path[:len(ancestor_path)] == ancestor_path
        results = filter(is_descendant, results)
      
      operators = {datastore_pb.Query_Filter.LESS_THAN:             '<',
                   datastore_pb.Query_Filter.LESS_THAN_OR_EQUAL:    '<=',
                   datastore_pb.Query_Filter.GREATER_THAN:          '>',
                   datastore_pb.Query_Filter.GREATER_THAN_OR_EQUAL: '>=',
                   datastore_pb.Query_Filter.EQUAL:                 '==',
                   }

      for filt in query.filter_list():
        assert filt.op() != datastore_pb.Query_Filter.IN

        prop = filt.property(0).name().decode('utf-8')
        op = operators[filt.op()]

        def passes(entity):
          """ Returns True if the entity passes the filter, False otherwise. """
          entity_vals = entity.get(prop, [])
          if type(entity_vals) != types.ListType:
            entity_vals = [entity_vals]

          entity_property_list = [datastore_types.ToPropertyPb(prop, value) for value in entity_vals]

          for entity_prop in entity_property_list:
            fixed_entity_val = datastore_types.FromPropertyPb(entity_prop)

            for filter_prop in filt.property_list():
              filter_val = datastore_types.FromPropertyPb(filter_prop)
              comp = u'%r %s %r' % (fixed_entity_val, op, filter_val)
              #logger.debug('Evaling filter expression "%s"' % comp )
              if eval(comp):
                return True
          return False

        results = filter(passes, results)

      for order in query.order_list():
        prop = order.property().decode('utf-8')
        #prop = str(order.property())
        if not prop == '__key__': 
          results = [entity for entity in results if prop in entity]

      def order_compare_entities(a, b):
        """ Return a negative, zero or positive number depending on whether
        entity a is considered smaller than, equal to, or larger than b,
        according to the query's orderings. """
        cmped = 0
        for o in orders:
          prop = o.property().decode('utf-8')

          if prop == '__key__':
            continue

          reverse = (o.direction() is datastore_pb.Query_Order.DESCENDING)

          a_val = datastore._GetPropertyValue(a, prop)
          if isinstance(a_val, list):
            a_val = sorted(a_val, order_compare_properties, reverse=reverse)[0]

          b_val = datastore._GetPropertyValue(b, prop)
          if isinstance(b_val, list):
            b_val = sorted(b_val, order_compare_properties, reverse=reverse)[0]

          cmped = order_compare_properties(a_val, b_val)

          if o.direction() is datastore_pb.Query_Order.DESCENDING:
            cmped = -cmped

          if cmped != 0:
            return cmped

        if cmped == 0:
          return cmp(a.key(), b.key())


      def order_compare_entities_pb(a, b):
        """ Return a negative, zero or positive number depending on whether
        entity a is considered smaller than, equal to, or larger than b,
        according to the query's orderings. a and b are protobuf-encoded
        entities."""
        return order_compare_entities(datastore.Entity.FromPb(a),
                                      datastore.Entity.FromPb(b))

      def order_compare_properties(x, y):
        """Return a negative, zero or positive number depending on whether
        property value x is considered smaller than, equal to, or larger than
        property value y. If x and y are different types, they're compared based
        on the type ordering used in the real datastore, which is based on the
        tag numbers in the PropertyValue PB.
        """
        if isinstance(x, datetime.datetime):
          x = datastore_types.DatetimeToTimestamp(x)
        if isinstance(y, datetime.datetime):
          y = datastore_types.DatetimeToTimestamp(y)

        x_type = _PROPERTY_TYPE_TAGS.get(x.__class__)
        y_type = _PROPERTY_TYPE_TAGS.get(y.__class__)

        if x_type == y_type:
          try:
            return cmp(x, y)
          except TypeError:
            return 0
        else:
          return cmp(x_type, y_type)

      results.sort(order_compare_entities)
      #limit = query.limit()
      #offset = query.offset() 
      #if query.has_limit():
      #  results = results[:query.limit()]

      #logger.info("****results after filtering:****")
      #logger.info("%s" % results)

    results = [ent._ToPb() for ent in results]
    for result in results:
      datastore_stub_util.PrepareSpecialPropertiesForLoad(result)

    # Pack Results into a clone of QueryResult #
    clone_qr_pb = datastore_pb.QueryResult()
    for res in results:
      clone_qr_pb.add_result()
      clone_qr_pb.result_[-1] = res

    clone_qr_pb.clear_cursor()
    clone_qr_pb.set_more_results( len(results)>0 )
    #logger.debug("QUERY_RESULT: %s" % clone_qr_pb)
    return (clone_qr_pb.Encode(), 0, "")


  def begin_transaction_request(self, app_id, http_request_data):
    transaction_pb = datastore_pb.Transaction()
    handle = generate_unique_id(app_id, None, None)
    #print "Begin Trans Handle:",handle
    transaction_pb.set_app(app_id)
    transaction_pb.set_handle(handle)
    app_datastore.setupTransaction(handle)
    return (transaction_pb.Encode(), 0, "")

  def commit_transaction_request(self, app_id, http_request_data):
    transaction_pb = datastore_pb.Transaction(http_request_data)
    handle = transaction_pb.handle() 
    commitres_pb = datastore_pb.CommitResponse()
    try:
      app_datastore.commit(handle)
    except:
      return (commitres_pb.Encode(), datastore_pb.Error.PERMISSION_DENIED, "Unable to commit for this transaction")  
    return (commitres_pb.Encode(), 0, "")

  def rollback_transaction_request(self, app_id, http_request_data):
    transaction_pb = datastore_pb.Transaction(http_request_data)
    handle = transaction_pb.handle() 
    try:
      app_datastore.rollback(handle)
    except:
      return(api_base_pb.VoidProto().Encode(), datastore_pb.Error.PERMISSION_DENIED, "Unable to rollback for this transaction")
    return (api_base_pb.VoidProto().Encode(), 0, "")


  def allocate_ids_request(self, app_id, http_request_data): # kowshik
    #logger.info("inside allocate_ids_request handler")
    request = datastore_pb.AllocateIdsRequest(http_request_data)
    response = datastore_pb.AllocateIdsResponse()
    #logger.info("created request and response objects")
    # The highest key
    highest_key = request.size()
    # Reference, which holds path info
    reference = request.model_key()  
    # get the root key and child key if it exists
    root_key = getRootKeyFromRef(app_id,reference)
    isChild = False
    child_key = None
    if root_key:
      isChild = True;
    if isChild:
      last_path = reference.path().element_list()[-1]
      uid = last_path.id()
      kind = last_path.type()
      # this object has no assigned id thus far
      if last_path.id() == 0 and not last_path.has_name():
        if root_key:
          # This child will not have a key assigned to it, just the type
          child_key = root_key + "/" + last_path.type()
        else:
          # If there is no child or root, then we use the global key 
          # generator. See generate_unique_id for more details
          # on key assignment
          child_key = None
    # Here we are just assigning just one key for uploading purposes
    # There needs to be a different generate function for doing allocations
    # that are larger than one

    # we could not use generate_unique_id because it should not be cached.
    #logger.info("just before attempting to allocate ids with zookeeper")
    uid = 0
    blockSize = 0
    while uid + blockSize <= highest_key and uid >= 0:
      if child_key:
        uid, blockSize = zoo_keeper.generateIDBlock(app_id, root_key)
      else:
        uid, blockSize = zoo_keeper.generateIDBlock(app_id)

    if uid < 0:
     logger.info("Failed!") 
     return (request.Encode(),
               datastore_pb.Error.INTERNAL_ERROR,
              'Allocation of id failed.')

    response.set_start(uid)
    response.set_end(uid)
    return (response.Encode(), 0, "")


  # Returns Null on error
  def getRootKeyFromEntity(self, app_id, entity):
    key = entity.key()
    if str(key.__class__) == "google.appengine.datastore.entity_pb.Reference":
      return getRootKeyFromRef(app_id, key)
    else:
      return getRootKeyFromKeyType(app_id, key)

  
  # For transactions
  # Verifies all puts are apart of the same root entity
  def getRootKeyFromTransPut(self, app_id, putreq_pb):
    ent_list = []
    if putreq_pb.entity_size() > 0:
      ent_list = putreq_pb.entity_list()
    first_ent = ent_list[0]
    expected_root = self.getRootKeyFromEntity(app_id, first_ent)
    # It is possible that all roots are None
    # because it is a root that has not gotten a uid

    for e in ent_list:
      root = self.getRootKeyFromEntity(app_id, e) 
      if root != expected_root:
        errcode = datastore_pb.Error.BAD_REQUEST
        errdetail = "All puts must be a part of the same group"
        return (None, errcode, errdetail)

    return (expected_root, 0, "")


  # For transactions
  # Verifies all puts are apart of the same root entity
  def getRootKeyFromTransReq(self, app_id, req_pb):
    if req_pb.key_size() <= 0:
      errcode = datastore_pb.error.bad_request
      errdetail = "Bad key listing"
      return (None, errcode, errdetail)

    key_list = req_pb.key_list()
    first_key = key_list[0]

    expected_root = getRootKeyFromRef(app_id, first_key)
    # It is possible that all roots are None
    # because it is a root that has not gotten a uid

    for k in key_list:
      root = getRootKeyFromRef(app_id, k) 
      if root != expected_root:
        errcode = datastore_pb.error.bad_request
        errdetail = "all transaction gets must be a part of the same group"
        return (None, errcode, errdetail)

    return (expected_root, 0, "")

  def getRootKeyFromTransGet(self, app_id, get_pb):
    return self.getRootKeyFromTransReq(app_id, get_pb)

  def getRootKeyFromTransDel(self, app_id, del_pb):
    return self.getRootKeyFromTransReq(app_id, del_pb)

  def put_request(self, app_id, http_request_data):
    global app_datastore
    global keySecret
    global tableHashTable

    field_name_list = []
    field_value_list = []

    start_time = time.time() 
    putreq_pb = datastore_pb.PutRequest(http_request_data)
    logger.debug("RECEIVED PUT_REQUEST %s" % putreq_pb)
    putresp_pb = datastore_pb.PutResponse( )
    txn = None
    root_key = None
    # Must assign an id if a put is being done in a transaction
    # and it does not have an id and it is a root
    for e in putreq_pb.entity_list():
      # Only dealing with root puts
      if e.key().path().element_size() == 1:
        root_path = e.key().path().mutable_element(0)
        #print "has id:",root_path.has_id(), "has name:",root_path.has_name()
        if root_path.id() == 0 and not root_path.has_name():
          #new_key = root_key + "/" + last_path.type()
          uid = generate_unique_id(app_id, None, None)
          #print "Assigned uid to new root key:",str(uid)
          if uid <= 0:
            return (putresp_pb.Encode(), 
                    datastore_pb.Error.INTERNAL_ERROR,
                    'Unable to assign a unique id')
          root_path.set_id(uid)

    # Gather data from Put Request #
    #print "Entity list for put:"
    #print putreq_pb.entity_list()
    for e in putreq_pb.entity_list():

      for prop in e.property_list() + e.raw_property_list():
        if prop.value().has_uservalue():
          obuid = md5.new(prop.value().uservalue().email().lower()).digest()
          obuid = '1' + ''.join(['%02d' % ord(x) for x in obuid])[:20]
          prop.mutable_value().mutable_uservalue().set_obfuscated_gaiaid(
              obuid)

      #################################
      # Key Assignment for new entities
      #################################  
      e.mutable_key().set_app(app_id)

      root_type = e.key().path().element(0).type()
      last_path = e.key().path().element_list()[-1]
      uid = last_path.id() 
      kind = last_path.type()
      # this object has no assigned id thus far
      if last_path.id() == 0 and not last_path.has_name():
        if e.key().path().element_size() == 1:
          root_key = None
        if root_key:
          child_key = root_key + "/" + last_path.type()
        else:
          child_key = None
        # if the root is None or the child is None, 
        # then the global counter is used
        # gen unique id only wants to know if a child exist
        uid = generate_unique_id(app_id, root_key, child_key)
        if uid <= 0: 
          return(putresp_pb.Encode(), 
                 datastore_pb.Error.INTERNAL_ERROR, 
                 "Unable to assign id to entity")
        last_path.set_id(uid)  
        # It may be its own parent
        group = e.mutable_entity_group()
        root = e.key().path().element(0)
        group.add_element().CopyFrom(root)
      if last_path.has_name():
        uid = last_path.name()
        # It may be its own parent
        group = e.mutable_entity_group()
        if group.element_size() == 0:
          root = e.key().path().element(0)
          group.add_element().CopyFrom(root)
     
      #######################################
      # Done with key assignment
      # Notify Soap Server of any new tables
      #######################################
      #print "Putting of type:",kind,"with uid of",str(uid)
      # insert key 
      namespace = e.key().name_space()
      table_name = getTableName(app_id, kind, namespace)
      #print "Put Using table name:",table_name
      # Notify Users/Apps table if a new class is being added 
      if table_name not in tableHashTable:
        # This is the first time this pbserver has seen this table
        # Notify the User/Apps server via soap call
        # This function is reentrant
        # If the class was deleted, and added a second time there is no 
        # notifying the users/app server of its creation
        if tableServer.add_class(app_id, kind, namespace, keySecret) == "true":
          tableHashTable[table_name] = 1
        
      # Store One Entity #

      row_key = getRowKey(app_id, e.key().path().element_list())
      inter_time = time.time() 
      logger.debug("Time spent in put before datastore call: " + str(inter_time - start_time))

            
      field_name_list = ENTITY_TABLE_SCHEMA
      field_value_list =  [e.Encode(), NONEXISTANT_TRANSACTION]
      if putreq_pb.has_transaction():
        txn = putreq_pb.transaction()
        err, res = app_datastore.put_entity( table_name,
                                           row_key,
                                           field_name_list,
                                           field_value_list,
                                           txn.handle())
      else:
        err, res = app_datastore.put_entity( table_name, 
                                           row_key, 
                                           field_name_list, 
                                           field_value_list)

      if err not in ERROR_CODES:
        #_trans_set.purge(txn)
        return (putresp_pb.Encode(),
                datastore_pb.Error.INTERNAL_ERROR,
                err)
      
      putresp_pb.key_list().append(e.key())

      inter_time = time.time()
      logger.debug("Time spent in put after datastore call: " + str(inter_time - start_time))
    logger.debug( "PUT_RESPONSE:%s" % putresp_pb)
    return (putresp_pb.Encode(), 0, "")


  def get_request(self, app_id, http_request_data):
    global app_datastore
    getreq_pb = datastore_pb.GetRequest(http_request_data)
    logger.debug("GET_REQUEST: %s" % getreq_pb)
    getresp_pb = datastore_pb.GetResponse()

           
    for key in getreq_pb.key_list():
      key.set_app(app_id)
      last_path = key.path().element_list()[-1]

      if last_path.has_id():
        entity_id = last_path.id()

      if last_path.has_name():
        entity_id = last_path.name()

      if last_path.has_type():
        kind = last_path.type()
      namespace = key.name_space()
      table_name = getTableName(app_id, kind, namespace)
      row_key = getRowKey(app_id,key.path().element_list())
      #print "Get row key:",row_key
      if getreq_pb.has_transaction():
        txn = getreq_pb.transaction()
        r = app_datastore.get_entity( table_name, row_key, ENTITY_TABLE_SCHEMA, txn.handle())
      else:
        r = app_datastore.get_entity( table_name, row_key, ENTITY_TABLE_SCHEMA )
      err = r[0]
      if err not in ERROR_CODES or len(r) != 3: 
        r = ["",None,NONEXISTANT_TRANSACTION]
        print err
      entity = r[1]
      prev_version = long(r[2])
            
      group = getresp_pb.add_entity()
      if entity:
        e_pb = entity_pb.EntityProto( entity )
        group.mutable_entity().CopyFrom(e_pb)
        
    # Send Response #
    #print getresp_pb
    logger.debug("GET_RESPONSE: %s" % getresp_pb)
    return (getresp_pb.Encode(), 0, "")

  """ Deletes are just PUTs using a sentinal value of DELETED
      All deleted keys are DELETED/entity_group. This is for 
      rollback to know which entity group a possible failed
      transaction belongs to.
  """ 
  def delete_request(self, app_id, http_request_data):
    global app_datastore
    root_key = None
    txn = None
    logger.debug("DeleteRequest Received...")
    delreq_pb = datastore_pb.DeleteRequest( http_request_data )
    logger.debug("DELETE_REQUEST: %s" % delreq_pb)
    delresp_pb = api_base_pb.VoidProto() 
   
    for key in delreq_pb.key_list():
      key.set_app(app_id)
      last_path = key.path().element_list()[-1]
      if last_path.has_type():
        kind = last_path.type()
      namespace = key.name_space()
      row_key = getRowKey(app_id, key.path().element_list())


      table_name = getTableName(app_id, kind, namespace)
      if delreq_pb.has_transaction():
        txn = delreq_pb.transaction()
        res = app_datastore.delete_row( table_name, row_key, txn.handle())
      else:
        res = app_datastore.delete_row( table_name, 
                                           row_key) 
      err = res[0]
      logger.debug("Response from DB for delete request %s" % err)
      if err not in ERROR_CODES: 
        if DEBUG: print err
        return (delresp_pb.Encode(),
                datastore_pb.Error.INTERNAL_ERROR,
                err + ", Unable to delete row")
 
    return (delresp_pb.Encode(), 0, "")


  def optimized_delete_request(self, app_id, http_request_data):
    pass
  def run_optimized_query(self, app_id, http_request_data):
    return 
  def optimized_put_request(self, app_id, http_request_data):
    pass

  def void_proto(self, app_id, http_request_data):
    resp_pb = api_base_pb.VoidProto() 
    print "Got void"
    logger.debug("VOID_RESPONSE: %s to void" % resp_pb)
    return (resp_pb.Encode(), 0, "" )
  
  def str_proto(self, app_id, http_request_data):
    str_pb = api_base_pb.StringProto( http_request_data )
    composite_pb = datastore_pb.CompositeIndices()
    print "Got a string proto"
    print str_pb
    logger.debug("String proto received: %s"%str_pb)
    logger.debug("CompositeIndex response to string: %s" % composite_pb)
    return (composite_pb.Encode(), 0, "" )    
  
  def int64_proto(self, app_id, http_request_data):
    int64_pb = api_base_pb.Integer64Proto( http_request_data ) 
    resp_pb = api_base_pb.VoidProto()
    print "Got a int 64"
    print int64_pb
    logger.debug("Int64 proto received: %s"%int64_pb)
    logger.debug("VOID_RESPONSE to int64: %s" % resp_pb)
    return (resp_pb.Encode(), 0, "")
 
  def compositeindex_proto(self, app_id, http_request_data):
    compindex_pb = entity_pb.CompositeIndex( http_request_data)
    resp_pb = api_base_pb.VoidProto()
    print "Got Composite Index"
    print compindex_pb
    logger.debug("CompositeIndex proto recieved: %s"%str(compindex_pb))
    logger.debug("VOID_RESPONSE to composite index: %s" % resp_pb)
    return (resp_pb.Encode(), 0, "")

# Returns 0 on success, 1 on failure
  def create_index_tables(self, app_id):
    global app_datastore
    """table_name = "__" + app_id + "__" + "kind"
    columns = ["reference"]
    print "Building table: " + table_name
    returned = app_datastore.create_table( table_name, columns )
    err,res = returned
    if err not in ERROR_CODES:
      logger.debug("%s" % err)
      return 1
    """
    table_name = "__" + app_id + "__" + "single_prop_asc"
    print "Building table: " + table_name
    columns = ["reference"]
    returned = app_datastore.create_table( table_name, columns )
    err,res = returned
    if err not in ERROR_CODES:
      logger.debug("%s" % err)
      return 1

    table_name = "__" + app_id + "__" + "single_prop_desc"
    print "Building table: " + table_name
    returned = app_datastore.create_table( table_name, columns )
    err,res = returned
    if err not in ERROR_CODES:
      logger.debug("%s" % err)
      return 1
 
    table_name = "__" + app_id + "__" + "composite"
    print "Building table: " + table_name
    returned = app_datastore.create_table( table_name, columns )
    err,res = returned
    if err not in ERROR_CODES:
      logger.debug("%s" % err)
      return 1 
   
    return 0 

  ##############
  # OTHER TYPE #
  ##############
  def unknown_request(self, app_id, http_request_data, pb_type):
    logger.debug("Received Unknown Protocol Buffer %s" % pb_type )
    print "ERROR: Received Unknown Protocol Buffer <" + pb_type +">.",
    print "Nothing has been implemented to handle this Protocol Buffer type."
    print "http request data:"
    print http_request_data 
    print "http done"
    self.void_proto(app_id, http_request_data)

  
  #########################
  # POST Request Handling #
  #########################
  @tornado.web.asynchronous
  def post( self ):
    request = self.request
    http_request_data = request.body
    pb_type = request.headers['protocolbuffertype']
    app_data = request.headers['appdata']
    app_data  = app_data.split(':')

    if len(app_data) == 4:
      app_id, user_email, nick_name, auth_domain = app_data
      os.environ['AUTH_DOMAIN'] = auth_domain
      os.environ['USER_EMAIL'] = user_email
      os.environ['USER_NICKNAME'] = nick_name
      os.environ['APPLICATION_ID'] = app_id 
    elif len(app_data) == 1:
      app_id = app_data[0]
      os.environ['APPLICATION_ID'] = app_id 
    else:
      logger.debug("UNABLE TO EXTRACT APPLICATION DATA")
      return

    # Default HTTP Response Data #

    if pb_type == "Request":
      self.remote_request(app_id, http_request_data)
    else:
      self.unknown_request(app_id, http_request_data, pb_type)
    self.finish()    

    
def usage():
  print "AppScale Server" 
  print
  print "Options:"
  print "\t--certificate=<path-to-ssl-certificate>"
  print "\t--a=<soap server hostname> "
  print "\t--key for using keys from the soap server"
  print "\t--type=<hypertable, hbase, cassandra, mysql, mongodb>"
  print "\t--secret=<secrete to soap server>"
  print "\t--blocksize=<key-block-size>"
  print "\t--optimized_query"
  print "\t--no_encryption"
def main(argv):
  global app_datastore
  global getKeyFromServer
  global tableServer
  global keySecret
  global logOn
  global logFilePtr
  global optimizedQuery
  global soapServer
  global ERROR_CODES
  global VALID_DATASTORES
  global KEYBLOCKSIZE
  global zoo_keeper
  cert_file = CERT_LOCATION
  key_file = KEY_LOCATION
  db_type = "hypertable"
  port = DEFAULT_SSL_PORT
  isEncrypted = True
  try:
    opts, args = getopt.getopt( argv, "c:t:l:s:b:a:k:p:o:n:z:", 
                               ["certificate=", 
                                "type=", 
                                "log=", 
                                "secret=", 
                                "blocksize=", 
                                "soap=", 
                                "key", 
                                "port", 
                                "optimized_query",
                                "no_encryption",
                                "zoo_keeper"] )
  except getopt.GetoptError:
    usage()
    sys.exit(1)
  for opt, arg in opts:
    if opt in ("-c", "--certificate"):
      cert_file = arg
      print "Using cert..."
    elif opt in ("-k", "--key" ):
      getKeyFromServer = True
      print "Using key server..."
    elif  opt in ("-t", "--type"):
      db_type = arg
      print "Datastore type: ",db_type 
    elif opt in ("-s", "--secret"):
      keySecret = arg
      print "Secret set..."
    elif opt in ("-l", "--log"):
      logOn = True
      logFile = arg
      logFilePtr = open(logFile, "w")
      logFilePtr.write("# type, app, start, end\n")
    elif opt in ("-b", "--blocksize"):
      KEYBLOCKSIZE = arg
      print "Block size: ",KEYBLOCKSIZE
    elif opt in ("-a", "--soap"):
      soapServer = arg
    elif opt in ("-o", "--optimized_query"):
      optimizedQuery = True
    elif opt in ("-p", "--port"):
      port = int(arg)
    elif opt in ("-n", "--no_encryption"):
      isEncrypted = False
    elif opt in ("-z", "--zoo_keeper"):
      zoo_keeper_locations = arg      

  app_datastore = appscale_datastore.DatastoreFactory.getDatastore(db_type)
  ERROR_CODES = appscale_datastore.DatastoreFactory.error_codes()
  VALID_DATASTORES = appscale_datastore.DatastoreFactory.valid_datastores()
  if DEBUG: print "ERROR_CODES:"
  if DEBUG: print ERROR_CODES
  if DEBUG: print "VALID_DATASTORE:"
  if DEBUG: print VALID_DATASTORES
  if db_type in VALID_DATASTORES:
    logger.debug("Using datastore %s" % db_type)
  else:
    print "Unknown datastore "+ db_type
    exit(1)

  tableServer = SOAPpy.SOAPProxy("https://" + soapServer + ":" + str(keyPort))
  
  global keyDictionaryLock 
  zoo_keeper = zktransaction.ZKTransaction(zoo_keeper_locations)

  keyDictionaryLock = threading.Lock()
  if port == DEFAULT_SSL_PORT and not isEncrypted:
    port = DEFAULT_PORT
  pb_application = tornado.web.Application([
    (r"/*", MainHandler),
  ])
  server = tornado.httpserver.HTTPServer(pb_application)
  server.listen(port) 
  if not db_type == "timesten":
    # Stop running as root, security purposes #
    drop_privileges()

  while 1:
    try:
      # Start Server #
      tornado.ioloop.IOLoop.instance().start()
    except SSL.SSLError:
      logger.debug("\n\nUnexcepted input for AppScale-Secure-Server")
    except KeyboardInterrupt:
      print "Server interrupted by user, terminating..."
      exit(1)

if __name__ == '__main__':
  #cProfile.run("main(sys.argv[1:])")
  main(sys.argv[1:])