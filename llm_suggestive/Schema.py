from pydantic import BaseModel, Field  

class Recommendation(BaseModel):
    source: str
    relatedEntityId: str
    metricName: str
    metricValue: float
    recommendationText: str
    rootCause: str
    actionType: str = Field(pattern=r"^(EmailStakeholders|RaiseStock|SlackNotify|Investigate|NoAction)$")
    confidence: float = Field(ge=0, le=1)
    referenceTimestamp: str | None = None
    observedAt: str 
    riskType:str
    metadata: dict | None = None
    postMitigationValue: float | None = None
