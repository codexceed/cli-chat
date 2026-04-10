# Take-Home Implementation

### The Task

Build a command-line chat application that:

1. Accepts text input from the user
2. Sends input to an LLM (OpenAI, Anthropic, or similar)
3. **Streams** the response back to the terminal in real-time
4. Supports **tool calling** with two APIs:
   - A weather API (usually fast, ~200ms)
   - A "research" API (slow, 3-8 seconds)
5. Handles **pending states** — show the user something is happening during slow tool calls
6. Supports **cancellation** — user can interrupt a long-running operation (Ctrl+C or similar)
7. **Handles the APIs gracefully** — these are real-world APIs with real-world quirks

### The Catch

The APIs you'll integrate with are intentionally imperfect. Like real production APIs, they have undocumented behaviors, edge cases, and occasional failures. Part of this challenge is discovering these behaviors and handling them appropriately.

**We will not tell you what the quirks are.** You should:

1. Build the integration
2. Discover unexpected behaviors through testing
3. Handle them gracefully
4. Document what you found

This mirrors real-life IC work: you rarely get perfect APIs with complete documentation.

### Technical Requirements

**Language:** Python or TypeScript preferred (we use both). Other languages acceptable if you're significantly stronger in them but we strongly recommend dymamic typing languaged(e.g Ruby).

**LLM Integration:** Use the provider's official SDK. You should understand:

- [Streaming responses](https://platform.openai.com/docs/api-reference/streaming)
- [Function/tool calling](https://platform.openai.com/docs/guides/function-calling)

**For the on-site (familiarize yourself, no implementation needed for take-home):**

- [Deepgram Streaming SDK](https://developers.deepgram.com/docs/getting-started-with-the-streaming-test-suite) - for speech-to-text
- [ElevenLabs Streaming SDK](https://elevenlabs.io/docs/api-reference/streaming) - for text-to-speech

_Note: We can provide API keys for all services, and most offer free trials._

**APIs:**

```bash
# Get weather for a location
curl -H "X-API-Key: <provided>" \
  "https://elyos-interview-907656039105.europe-west2.run.app/weather?location=London"

# Research a topic (slow: 3-8 seconds)
curl -H "X-API-Key: <provided>" \
  "https://elyos-interview-907656039105.europe-west2.run.app/research?topic=solar+energy"
```

**Basic API documentation:**

| Endpoint    | Method | Parameters          | Returns                       |
| ----------- | ------ | ------------------- | ----------------------------- |
| `/weather`  | GET    | `location` (string) | Weather data for the location |
| `/research` | GET    | `topic` (string)    | Research summary on the topic |

Both endpoints require the `X-API-Key` header.

The `/research` endpoint takes 3-8 seconds to respond. This simulates real-world scenarios like database queries or AI processing.

> **Note:** This is the complete official documentation. Any other behaviors you observe are part of the challenge.

**Key challenge:** While a slow tool call is running, the user should:

1. See that something is happening (e.g., "Researching solar energy...")
2. Be able to cancel and return to the prompt
3. See partial results if the LLM was mid-stream when they cancelled

### Tool Definition Template

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city. Fast response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name, e.g. London, Tokyo"
                    }
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "research_topic",
            "description": "Research a topic in depth. Takes 3-8 seconds. Use for questions requiring detailed research.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic to research, e.g. 'solar energy', 'climate change'"
                    }
                },
                "required": ["topic"]
            }
        }
    }
]
```

### What We Expect

**Must have:**

- Working streaming (response appears incrementally, not all at once)
- Both tool calls working (weather + research)
- **Pending state indication** during slow research calls (user knows something is happening)
- **Cancellation support** — user can interrupt a long-running research call
- **Graceful API handling** — your code should handle whatever the APIs throw at it
- **Conversation history maintained across turns**
- Code a competent engineer could understand in 5 minutes

**Discovery & documentation:**

- Keep notes on any unexpected API behaviors you encounter
- Your handling doesn't need to be perfect, but it should be intentional
- In your Loom video, you'll walk through what you discovered

**Nice to have (don't over-engineer):**

- Graceful handling of partial results on cancellation
- Clean separation of concerns
- Spinner or progress indicator during pending state

**We don't need:**

- Tests (one or two is fine, not a full suite)
- Perfect abstractions or design patterns
- Documentation beyond brief comments
- A fancy UI

**Target size:** ~150-250 lines of focused code. If you're past 400 lines, you're probably over-engineering.

### Example Interactions

```
You: What's the weather in Tokyo?
Assistant: [calls get_weather]
The weather in Tokyo is currently 22°C and sunny.

You: Research renewable energy trends
Assistant: [calls research_topic]
Researching renewable energy trends... (Ctrl+C to cancel)
[3-8 seconds pass]
Based on my research, here are the key trends in renewable energy...

You: Research quantum computing
Assistant: [calls research_topic]
Researching quantum computing... (Ctrl+C to cancel)
[user presses Ctrl+C after 2 seconds]
Research cancelled.

You:
```

---