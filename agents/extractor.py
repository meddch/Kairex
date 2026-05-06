import json

from openai import OpenAI

SYSTEM_PROMPT = """You are an organizational knowledge extractor.
Given a conversation transcript, extract structured facts about the organization.

You MUST return a JSON object with a single key "facts" whose value is an array.
Each element of the array must have exactly these keys:
- entity_type: string (e.g. "company", "person", "product", "team", "process")
- entity_value: string (the name/identifier of the entity)
- attribute: string (what aspect of the entity this fact describes)
- value: string (the actual fact value)
- confidence: float between 0 and 1
- source_snippet: string (the exact phrase from the transcript this was derived from)

Example response:
{
  "facts": [
    {
      "entity_type": "company",
      "entity_value": "Acme Corp",
      "attribute": "industry",
      "value": "software",
      "confidence": 0.95,
      "source_snippet": "we build software for enterprise clients"
    }
  ]
}

Extract only clearly stated facts. Do not infer or hallucinate. Return {"facts": []} if nothing is found."""


class ExtractorAgent:
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    def extract(self, transcript: str, session_id: str, org_id: str) -> list[dict]:
        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Transcript:\n{transcript}"},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        parsed = json.loads(response.choices[0].message.content)
        raw_facts = parsed.get("facts", [])

        return [
            {
                "entity_type": f.get("entity_type", ""),
                "entity_value": f.get("entity_value", ""),
                "attribute": f.get("attribute", ""),
                "value": f.get("value", ""),
                "confidence": float(f.get("confidence", 0.0)),
                "source_snippet": f.get("source_snippet", ""),
            }
            for f in raw_facts
        ]
