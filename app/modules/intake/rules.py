from .reader import read_source
from ..dispense.reader import read_dispense
FIELDS=['id', 'subject', 'visit', 'at', 'enrolled', 'kind', 'parent', 'value', 'unit', 'protocol', 'revision']
def apply(state):
    read_source(state,FIELDS,{'编号':'id'})
    read_dispense(state)
