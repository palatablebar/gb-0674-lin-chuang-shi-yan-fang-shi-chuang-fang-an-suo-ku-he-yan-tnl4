from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
router=APIRouter()
@router.get('/dispense')
def page(request:Request):
    return render(request,'dispense')
@router.post('/api/dispense')
def calculate(payload:dict=Body(default={})):
    return evaluate(payload,until=6)
