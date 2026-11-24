"""Closed, explicit per-account document vector selection; no build authority."""
import copy

KEY='document_vector_profile'
MARKER='_document_vector_profile_version'
EXACT={'version':1,'family':'exact'}

def _integer(value,low,high):
    return type(value) is int and type(low) is int and type(high) is int and low<=value<=high

def _transform(value):
    if type(value) is not dict:return False
    if value=={'kind':'identity'}:return True
    return (set(value)=={'kind','seed'} and value.get('kind')=='centered_signed_permutation'
        and _integer(value.get('seed'),0,4294967295))

def validate(value):
    if type(value) is not dict or value.get('version')!=1 or type(value.get('version')) is not int:
        raise ValueError('document vector selection unavailable')
    family=value.get('family')
    if family=='exact':valid=set(value)=={'version','family'}
    elif family in ('ivf_flat','ivf_minibatch'):
        valid=(set(value)=={'version','family','seed','lists','iterations','effort','visit_budget'}
            and _integer(value.get('seed'),0,4294967295) and _integer(value.get('lists'),1,128)
            and _integer(value.get('iterations'),1,32) and _integer(value.get('effort'),1,value.get('lists',0))
            and _integer(value.get('visit_budget'),1,4096))
    elif family=='hnsw':
        valid=(set(value)=={'version','family','seed','m','ef_construction','effort','visit_budget'}
            and _integer(value.get('seed'),0,4294967295) and _integer(value.get('m'),2,32)
            and _integer(value.get('ef_construction'),value.get('m',257),256)
            and _integer(value.get('effort'),1,256) and _integer(value.get('visit_budget'),1,4096))
    elif family=='scalar8':
        valid=(set(value)=={'version','family','transform','candidate_limit'} and _transform(value.get('transform'))
            and _integer(value.get('candidate_limit'),1,1024))
    elif family=='pq':
        valid=(set(value)=={'version','family','seed','transform','parts','centroids','iterations','candidate_limit'}
            and _integer(value.get('seed'),0,4294967295) and _transform(value.get('transform'))
            and _integer(value.get('parts'),1,1024) and _integer(value.get('centroids'),2,256)
            and _integer(value.get('iterations'),1,32) and _integer(value.get('candidate_limit'),1,1024))
    else:valid=False
    if not valid:raise ValueError('document vector selection unavailable')
    return copy.deepcopy(value)

def selected(prefs):
    if type(prefs) is not dict:raise ValueError('document preferences unavailable')
    if prefs.get(MARKER)!=1 or type(prefs.get(MARKER)) is not int:return copy.deepcopy(EXACT)
    return validate(prefs.get(KEY))

def choice(selection):
    value=validate(selection);family=value['family']
    if family=='exact':return None
    fields={'ivf_minibatch':('family','seed','lists','iterations'),'ivf_flat':('family','seed','lists','iterations'),'hnsw':('family','seed','m','ef_construction'),
        'scalar8':('family','transform'),'pq':('family','seed','transform','parts','centroids','iterations')}[family]
    return {name:copy.deepcopy(value[name]) for name in fields}
