#!/usr/bin/python

import re
import json
import time
import datetime
import logging
import socket
import collections

import elasticsearch
import importlib.util

from . import convert


def filter_name(keys):
    for key in keys:
        if key.startswith("MATCH_EXP_JOB_"):
            key = key[len("MATCH_EXP_JOB_") :]
        if key.endswith("_RAW"):
            key = key[: -len("_RAW")]
        yield key


def make_mappings():
    props = {}
    for name in filter_name(convert.TEXT_ATTRS):
        props[name] = {"type": "text"}
    for name in filter_name(convert.INDEXED_KEYWORD_ATTRS):
        props[name] = {"type": "keyword"}
    for name in filter_name(convert.NOINDEX_KEYWORD_ATTRS):
        props[name] = {"type": "keyword", "index": "false"}
    for name in filter_name(convert.FLOAT_ATTRS):
        props[name] = {"type": "double"}
    for name in filter_name(convert.INT_ATTRS):
        props[name] = {"type": "long"}
    for name in filter_name(convert.DATE_ATTRS):
        props[name] = {"type": "date", "format": "epoch_second"}
    for name in filter_name(convert.BOOL_ATTRS):
        props[name] = {"type": "boolean"}
    props["metadata"] = {
        "properties": {"spider_runtime": {"type": "date", "format": "epoch_millis"}}
    }

    dynamic_templates = [
        {"strings_as_keywords": { # Store unknown strings as keywords
            "match_mapping_type": "string",
            "mapping": {
                "type": "keyword",
                "norms": "false",
                "ignore_above": 256
            }
        }},
        {"date_attrs": { # Attrs ending in "Date" are usually timestamps
            "match": "*Date",
            "mapping": {
                "type": "date",
                "format": "epoch_second"
            }
        }},
        {"resource_request_attrs": {  # Attrs starting with "Request" are
            "match_pattern": "regex", # usually resource numbers
            "match": "^Request[A-Z].*$",
            "mapping": {
                "type": "long",
            }
        }},
        {"target_boolean_attrs": {    # Attrs starting with "Want", "Has", or
            "match_pattern": "regex", # "Is" are usually boolean checks on the
            "match": "^(Want|Has|Is)[A-Z].*$", # target machine
            "mapping": {
                "type": "boolean"
            }
        }},
        {"raw_expressions": {  # Attrs ending in "_EXPR" are generated during
            "match": "*_EXPR", # ad conversion for expressions that cannot be
            "mapping": {       # evaluated.
                "type": "keyword",
                "index": "false"
            }
        }},
    ]

    mappings = {"dynamic_templates": dynamic_templates, "properties": props}
    return mappings


def make_settings():
    settings = {
        "analysis": {
            "analyzer": {
                "analyzer_keyword": {"tokenizer": "keyword", "filter": "lowercase"}
            }
        },
        "mapping.total_fields.limit": 2000,
    }
    return settings


_ES_HANDLE = None


def get_server_handle(args=None):
    global _ES_HANDLE
    if not _ES_HANDLE:
        if not args:
            logging.error(
                "Call get_server_handle with args first to create ES interface instance"
            )
            return _ES_HANDLE
        _ES_HANDLE = ElasticInterface(hostname=args.es_host, port=args.es_port,
          username=args.es_username, password=args.es_password, use_https=args.es_use_https)
    return _ES_HANDLE


class ElasticInterface(object):
    """Interface to elasticsearch"""

    def __init__(self, hostname="localhost", port=9200, username=None, password=None, use_https=False):

        es_client = {
            'host': hostname,
            'port': port,
        }

        if (username is None) and (password is None):
            # connect anonymously
            pass
        elif (username is None) != (password is None):
            logger.warning('Only one of username and password have been defined, attempting anonymous connection to Elasticsearch')
        else:
            es_client['http_auth'] = (username, password)

        if use_https:
            if importlib.util.find_spec('certifi') is None:
                logger.error('"certifi" library not found, cannot use HTTPS to connect to Elasticsearch')
                sys.exit(1)
            else:
                es_client['use_ssl'] = True
                es_client['verify_certs'] = True

        self.handle = elasticsearch.Elasticsearch([es_client])

    def fix_mapping(self, idx, template="htcondor"):
        idx_clt = elasticsearch.client.IndicesClient(self.handle)
        mappings = make_mappings()
        custom_mappings = {
            "CMSPrimaryDataTier": mappings["properties"]["CMSPrimaryDataTier"],
            "CMSPrimaryPrimaryDataset": mappings["properties"][
                "CMSPrimaryPrimaryDataset"
            ],
            "CMSPrimaryProcessedDataset": mappings["properties"][
                "CMSPrimaryProcessedDataset"
            ],
        }
        logging.info(
            idx_clt.put_mapping(
                index=idx, body=json.dumps({"properties": custom_mappings}), ignore=400
            )
        )

    def make_mapping(self, idx, template="htcondor"):
        idx_clt = elasticsearch.client.IndicesClient(self.handle)
        mappings = make_mappings()
        # print(idx_clt.put_mapping(index=idx, body=json.dumps({"properties": mappings}), ignore=400))
        settings = make_settings()
        # print(idx_clt.put_settings(index=idx, body=json.dumps(settings), ignore=400))

        body = json.dumps({"mappings": mappings, "settings": {"index": settings}})

        with open("last_mappings.json", "w") as jsonfile:
            json.dump(json.loads(body), jsonfile, indent=2, sort_keys=True)

        result = self.handle.indices.create(  # pylint: disable = unexpected-keyword-arg
            index=idx, body=body, ignore=400
        )
        if result.get("status") != 400:
            logging.warning(f"Creation of index {idx}: {str(result)}")
        elif "already exists" not in result.get("error", "").get("reason", ""):
            logging.error(
                f'Creation of index {idx} failed: {str(result.get("error", ""))}'
            )


_INDEX_CACHE = set()


def get_index(timestamp, template="htcondor", update_es=True):
    global _INDEX_CACHE
    idx = time.strftime(
        "%s-%%Y-%%m-%%d" % template,
        datetime.datetime.utcfromtimestamp(timestamp).timetuple(),
    )

    if update_es:
        if idx in _INDEX_CACHE:
            return idx

        _es_handle = get_server_handle()
        _es_handle.make_mapping(idx, template=template)
        _INDEX_CACHE.add(idx)

    return idx


def make_es_body(ads, metadata=None):
    metadata = metadata or {}
    body = ""
    for id_, ad in ads:
        if metadata:
            ad.setdefault("metadata", {}).update(metadata)

        body += json.dumps({"index": {"_id": id_}}) + "\n"
        body += json.dumps(ad) + "\n"

    return body


def parse_errors(result):
    reasons = [
        d.get("index", {}).get("error", {}).get("reason", None) for d in result["items"]
    ]
    counts = collections.Counter([_f for _f in reasons if _f])
    n_failed = sum(counts.values())
    logging.error(
        f"Failed to index {n_failed:d} documents to ES: {str(counts.most_common(3))}"
    )
    return n_failed


def post_ads(es, idx, ads, metadata=None):
    body = make_es_body(ads, metadata)
    res = es.bulk(body=body, index=idx, request_timeout=60)
    if res.get("errors"):
        return parse_errors(res)


def post_ads_nohandle(idx, ads, args, metadata=None):
    es = get_server_handle(args).handle
    body = make_es_body(ads, metadata)
    res = es.bulk(body=body, index=idx, request_timeout=60)
    if res.get("errors"):
        return parse_errors(res)

    return len(ads)
