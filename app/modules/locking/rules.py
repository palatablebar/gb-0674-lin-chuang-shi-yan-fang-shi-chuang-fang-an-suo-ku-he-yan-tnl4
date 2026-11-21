def decide(state):
    c=state['counts'];explained=len(state['records'])+len(state['quarantine'])+c['blank']+c['duplicate']+c['repeated_header']
    dispense=state.get('dispense') or {}
    dq=dispense.get('quarantine',[])
    blocked=len(state['deviations'])+len(state['retest_issues'])+len(state['quarantine'])
    # 发药结论里“访视未完成”的那部分与偏离模块同源，只计一次；只把药盒级的新结论加进总量。
    dispense_blocked=dispense.get('own_blocked_visits',0)+len(dq)+(1 if dispense.get('file_issue') else 0)
    handover_ok=bool(dispense) and dispense.get('status')=='可放行'
    return {'input':c['input'],'accepted':len(state['records']),'quarantined':len(state['quarantine']),
            'row_balanced':c['input']==explained,'completed':len(state['completed']),
            'expected':len(state['context']['subjects'])*len(state['context']['required_visits']),
            'deviations':len(state['deviations']),'retest_issues':len(state['retest_issues']),
            'dispense_blocked':dispense_blocked,'dispense_quarantined':len(dq),
            'settled_visits':dispense.get('settled_visits',0),'handover_status':dispense.get('status',''),
            'handover_ok':handover_ok,'blocked':blocked,
            'blocked_total':blocked+dispense_blocked,
            'status':'可锁库' if not blocked and c['input']==explained and handover_ok else '待处理'}
def apply(state):
    state['balance']=decide(state);state['views']['locking']={'title':'锁库闸门','rows':[state['balance']],'metrics':{'应访视':state['balance']['expected'],'已完成':state['balance']['completed'],'阻断项':state['balance']['blocked_total'],'交接阻断':state['balance']['dispense_blocked']}}
