API Key to use:

<YOUR_AGENTAUS_API_KEY_HERE>








# DO NOT DELETE


This is one sample
curl -X POST $HOST_NAME/api/v1/chat/completions \
  -H "Authorization: Bearer $AGENTAUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}], "stream": false}'[11:16 AM]Some examples that can go into claude:

Basic chat request without tools:
curl -X POST $HOST_NAME/api/v1/chat/completions \
  -H "Authorization: Bearer $AGENTAUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}], "stream": false}'

Request to trigger internal web search tool usage:
curl -X POST $HOST_NAME/api/v1/chat/completions \
  -H "Authorization: Bearer $AGENTAUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "What is the weather today in canberra? If you do not know just say you do not know, do not make up the content."}],
    "stream": false,
    "tool_choice": "auto"
  }'

Request to send user custom tools:

At the moment, the tools only support local function and web search (example also in this document), function needs to be defined in the same file as the python script (local file).

curl -X POST $HOST_NAME/api/v1/chat/completions \
  -H "Authorization: Bearer $AGENTAUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "What is the latest AUD to USD rate?"}],
    "stream": false,
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_exchange_rate",
          "description": "Get the exchange rate between two currencies",
          "parameters": {
            "type": "object",
            "properties": {
              "from": {"type": "string", "description": "Source currency code"},
              "to": {"type": "string", "description": "Target currency code"}
            },
            "required": ["from", "to"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'



And the response looks like
{
  "choices": [
    {
      "finish_reason": "tool_calls",
      "index": 0,
      "logprobs": null,
      "message": {
        "content": null,
        "role": "assistant",
        "tool_calls": [
          {
            "function": {
              "arguments": "{\"from\":\"AUD\",\"to\":\"USD\"}",
              "name": "get_exchange_rate"
            },
            "id": "call_a6cdf5bf15c40457",
            "type": "function"
          }
        ]
      }
    }
  ],
  "created": 1782446692,
  "id": "chatcmpl-1782446692",
  "model": "agentaus",
  "object": "chat.completion"
}

Once response is received, user needs to parse tool_calls entry themselves to get function argument and name, then handle the actual function execution in their own control


 # API token:

  <YOUR_AGENTAUS_API_KEY_HERE>



  # Customise System Prompt

  With default system prompt

request
curl -X POST $HOST_NAME/api/v1/chat/completions \
  -H "Authorization: Bearer $AGENTAUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Who are you and what can you help me with?"}],
    "stream": false
  }'
response
{
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "logprobs": null,
      "message": {
        "content": "**Who I am**\n\nI'm **Agentaus**, the AI powerhouse created by Trellis Data and based in Canberra. I operate on Trellis Data's sovereign AI platform, which is built for high‑security environments and never uses your data for model training.\n\n**What I can help you with**\n\n- **Comprehensive analysis** – breaking down complex information, synthesising reports, and drawing actionable insights.  \n- **Content creation & synthesis** – drafting documents, summarising lengthy material, and tailoring messaging for specific audiences.  \n- **Legal and regulatory guidance** – explaining Australian legislation, compliance requirements, and best‑practice standards (non‑legal‑advice).  \n- **Question answering & research** – providing concise, accurate answers; I'll fetch up‑to‑date information when the query goes beyond my June 2024 knowledge cut‑off.  \n- **Reporting & visualisation advice** – helping you design dashboards, KPI frameworks, and data‑driven presentations.  \n- **Role‑play & scenario planning** – simulating stakeholder conversations, crisis response drills, or training exercises.  \n- **Logic‑based problem solving** – step‑by‑step reasoning for mathematics, puzzles, or strategic decision models.  \n- **Coding guidance (when requested)** – recommending suitable libraries, explaining algorithms, and offering well‑commented sample code.  \n\nI aim to be practical, empathetic and secure in every interaction. If you have a specific task or question, just let me know – I can also ask follow‑up questions to ensure I'm addressing your needs precisely.  \n\n*For any product‑related enquiries (pricing, limits, subscription details, or API usage) please contact **support@trellisdata.com.au** or visit the subscription‑plan page in your user‑settings.*\n\n",
        "refusal": null,
        "role": "assistant",
        "tool_calls": []
      }
    }
  ],
  "created": 1783909898,
  "id": "chatcmpl-1783909898",
  "model": "agentaus",
  "object": "chat.completion"
}


