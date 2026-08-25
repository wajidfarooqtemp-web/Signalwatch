# boolean_search.py
#
# A real boolean query parser and evaluator for AND, OR, NOT,
# parentheses, and quoted exact phrases.
#
# This replaces the previous approach in app.py, where the words AND
# and OR were silently stripped out as stop words before scoring, and
# parentheses were never read at all. A query like
# "(Salesforce OR Intercom) AND pricing" was being treated as three
# unrelated words, any one of which was enough to match. This file
# builds an actual expression tree from the query and evaluates it
# properly against each result.
#
# Grammar, lowest precedence first:
#   expression := or_expr
#   or_expr    := and_expr (OR and_expr)*
#   and_expr   := not_expr (AND? not_expr)*   (AND is optional; two
#                 terms with nothing between them are implicit AND)
#   not_expr   := NOT primary | primary
#   primary    := "(" expression ")" | PHRASE | WORD
#
# Two words with nothing between them default to AND, since that is
# what a person typing "Nike pricing" actually means.

import re


class TermNode:
    """A leaf node: one word or one exact phrase to match against a title."""
    def __init__(self, text, is_phrase):
        self.text = text.lower()
        self.is_phrase = is_phrase

    def evaluate(self, text_lower):
        return self.text in text_lower

    def collect_terms(self, negated=False):
        # Used for scoring and for building a plain English explanation.
        # Negated terms are never counted toward the score, since
        # matching a NOT'd word should never make a result rank higher.
        if negated:
            return []
        return [(self.text, self.is_phrase)]


class NotNode:
    def __init__(self, child):
        self.child = child

    def evaluate(self, text_lower):
        return not self.child.evaluate(text_lower)

    def collect_terms(self, negated=False):
        # Flip negated state as we descend past a NOT
        return self.child.collect_terms(negated=not negated)


class AndNode:
    def __init__(self, left, right):
        self.left = left
        self.right = right

    def evaluate(self, text_lower):
        return self.left.evaluate(text_lower) and self.right.evaluate(text_lower)

    def collect_terms(self, negated=False):
        return self.left.collect_terms(negated) + self.right.collect_terms(negated)


class OrNode:
    def __init__(self, left, right):
        self.left = left
        self.right = right

    def evaluate(self, text_lower):
        return self.left.evaluate(text_lower) or self.right.evaluate(text_lower)

    def collect_terms(self, negated=False):
        return self.left.collect_terms(negated) + self.right.collect_terms(negated)


def _tokenize(query: str) -> list:
    """
    Turns a raw query string into a flat list of tokens.

    Quoted phrases are pulled out first and replaced with a placeholder,
    so a phrase like "customer service" is never split into two
    separate word tokens. Parentheses are padded with spaces so they
    always tokenize as their own token, even when written with no
    space, like "(Nike".
    """
    phrases = []

    def stash_phrase(match):
        phrases.append(match.group(1))
        return f" __PHRASE_{len(phrases) - 1}__ "

    working = re.sub(r'"([^"]*)"', stash_phrase, query)
    working = working.replace("(", " ( ").replace(")", " ) ")

    raw_tokens = working.split()
    tokens = []
    for tok in raw_tokens:
        phrase_match = re.match(r'^__PHRASE_(\d+)__$', tok)
        if phrase_match:
            idx = int(phrase_match.group(1))
            tokens.append(("PHRASE", phrases[idx]))
        elif tok == "(":
            tokens.append(("LPAREN", tok))
        elif tok == ")":
            tokens.append(("RPAREN", tok))
        elif tok.upper() == "AND":
            tokens.append(("AND", tok))
        elif tok.upper() == "OR":
            tokens.append(("OR", tok))
        elif tok.upper() == "NOT":
            tokens.append(("NOT", tok))
        else:
            tokens.append(("WORD", tok))
    return tokens


class _Parser:
    """Simple recursive descent parser over the token list."""
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def advance(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def parse(self):
        if not self.tokens:
            return None
        node = self.parse_or()
        return node

    def parse_or(self):
        left = self.parse_and()
        while self.peek()[0] == "OR":
            self.advance()
            right = self.parse_and()
            if right is not None:
                left = OrNode(left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while True:
            kind = self.peek()[0]
            if kind == "AND":
                self.advance()
                right = self.parse_not()
                if right is not None:
                    left = AndNode(left, right)
            elif kind in ("WORD", "PHRASE", "LPAREN", "NOT"):
                # No explicit operator between two terms means implicit AND,
                # e.g. "Nike pricing" means Nike AND pricing.
                right = self.parse_not()
                if right is not None:
                    left = AndNode(left, right)
            else:
                break
        return left

    def parse_not(self):
        if self.peek()[0] == "NOT":
            self.advance()
            child = self.parse_primary()
            if child is None:
                return None
            return NotNode(child)
        return self.parse_primary()

    def parse_primary(self):
        kind, value = self.peek()
        if kind == "LPAREN":
            self.advance()
            node = self.parse_or()
            if self.peek()[0] == "RPAREN":
                self.advance()
            return node
        if kind == "WORD":
            self.advance()
            return TermNode(value, is_phrase=False)
        if kind == "PHRASE":
            self.advance()
            return TermNode(value, is_phrase=True)
        # RPAREN, OR, AND with nothing valid before it, or end of input
        return None


def parse_boolean_query(query: str):
    """
    Parses a raw query string into an evaluatable expression tree.
    Returns None if the query is empty or contains no real terms
    (for example, a query that was only "AND OR NOT" with nothing else).
    """
    if not query or not query.strip():
        return None
    tokens = _tokenize(query)
    parser = _Parser(tokens)
    return parser.parse()


def matches(tree, title: str) -> bool:
    """
    Returns True if this title satisfies the boolean expression.
    A tree of None (empty or malformed query) matches nothing, which
    is the safe default, since showing everything for a broken query
    would be worse than showing nothing.
    """
    if tree is None:
        return False
    return tree.evaluate(title.lower())


def score(tree, title: str) -> int:
    """
    Scores a title that has already passed matches(). Scoring only
    counts non-negated terms, so a NOT'd word never contributes to
    ranking, only to exclusion. Phrases are worth more than single
    words, matching the old scoring's phrase bonus.
    """
    if tree is None:
        return 0
    text_lower = title.lower()
    total = 0
    terms = tree.collect_terms()
    for text, is_phrase in terms:
        count = text_lower.count(text)
        total += count * (5 if is_phrase else 2)
    return total


def explain(tree, title: str) -> str:
    """
    Plain English explanation of why a result matched, for the same
    score_reason field the UI already shows.
    """
    if tree is None:
        return ""
    text_lower = title.lower()
    reasons = []
    for text, is_phrase in tree.collect_terms():
        if text in text_lower:
            if is_phrase:
                reasons.append(f'exact phrase: "{text}"')
            else:
                count = text_lower.count(text)
                reasons.append(f"contains '{text}'" if count == 1 else f"mentions '{text}' {count} times")
    if not reasons:
        return ""
    return "Ranked high: " + ", ".join(reasons)