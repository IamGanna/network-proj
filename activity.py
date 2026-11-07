"""In-chat activities typed as chat text: /coin, /dice, /rng, /poll and /vote.

The server picks every result, so everyone in a room sees the same one.
apply() runs under server.lock, which also guards the poll state below.
"""
import secrets

MAX_POLLS_PER_ROOM = 10  # a chat forgets its oldest polls, so spamming one chat can't wipe another's
MAX_POLLS = 1000  # overall safety net on memory
polls = {}  # poll id -> {"room", "question", "options", "votes": {voter: option index}}
next_id = 1


def apply(sender, room, text):
    """Return the text to deliver: a command's result, or the text unchanged."""
    word, _, rest = text.strip().partition(" ")
    command = word.lower()
    if command == "/coin":
        return f"🪙 flipped a coin: {secrets.choice(['Heads', 'Tails'])}"
    if command == "/dice":
        return f"🎲 rolled a die: {secrets.randbelow(6) + 1}"
    if command == "/rng":
        return rng(rest)
    if command == "/poll":
        return create_poll(room, rest)
    if command == "/vote":
        return vote(sender, room, rest)
    return text


def forget(room):
    """Drop a room's polls, so a new group reusing the name starts fresh."""
    for poll_id in [poll_id for poll_id, poll in polls.items() if poll["room"] == room]:
        del polls[poll_id]


def rng(rest):
    try:
        low, high = map(int, rest.split()) if rest.strip() else (1, 100)
    except ValueError:
        return "⚠️ Usage: /rng  or  /rng <min> <max>"
    if not -10**12 <= low < high <= 10**12:
        return "⚠️ /rng needs min < max, both within ±1,000,000,000,000"
    return f"🔢 random number from {low} to {high}: {low + secrets.randbelow(high - low + 1)}"


def create_poll(room, rest):
    global next_id
    question, *options = [part.strip() for part in rest.split("|")]
    options = [option for option in options if option]
    if not question or not 2 <= len(options) <= 10 or len(question) > 200 or max(map(len, options)) > 100:
        return "⚠️ Usage: /poll Question | option 1 | option 2  (2 to 10 options)"
    poll_id, next_id = next_id, next_id + 1
    polls[poll_id] = {"room": room, "question": question, "options": options, "votes": {}}
    in_room = [old_id for old_id, poll in polls.items() if poll["room"] == room]
    if len(in_room) > MAX_POLLS_PER_ROOM:
        del polls[in_room[0]]
    if len(polls) > MAX_POLLS:
        del polls[next(iter(polls))]
    return tally(poll_id, f"📊 Poll #{poll_id}") + f"\nVote with: /vote {poll_id} <number>"


def vote(sender, room, rest):
    try:
        poll_id, choice = map(int, rest.split())
    except ValueError:
        return "⚠️ Usage: /vote <poll number> <option number>"
    poll = polls.get(poll_id)
    if poll is None or poll["room"] != room:  # a vote only counts in the chat that started the poll
        return f"⚠️ There's no poll #{poll_id} in this chat"
    if not 1 <= choice <= len(poll["options"]):
        return f"⚠️ Poll #{poll_id} has options 1 to {len(poll['options'])}"
    poll["votes"][sender] = choice - 1  # one vote per person; voting again changes it
    return tally(poll_id, f"🗳️ voted in poll #{poll_id}")


def tally(poll_id, title):
    poll = polls[poll_id]
    counts = [0] * len(poll["options"])
    for choice in poll["votes"].values():
        counts[choice] += 1
    lines = [title, poll["question"]]
    lines += [f"{i}. {option}: {n} vote{'' if n == 1 else 's'}"
              for i, (option, n) in enumerate(zip(poll["options"], counts), 1)]
    return "\n".join(lines)
