"""本机内置知识库只读目录。"""
from fastapi import APIRouter, Query
import evidence

router=APIRouter()


@router.get('/api/knowledge')
def knowledge_catalog(domain: str='', age: str='', q: str=Query('',max_length=100), limit: int=Query(120,ge=1,le=500), source: str=''):
    return evidence.browse(domain,age,q,limit,source)
