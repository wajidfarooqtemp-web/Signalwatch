# Signalwatch

Brand intelligence platform. Signalwatch pulls public mentions of a brand
from many sources, ranks them, and produces a short briefing that tells
you what is happening and what to do about it. It is not a dashboard
full of charts. It is meant to save the hour someone would otherwise
spend reading through mentions by hand.

## What it actually does

A person types a query. Signalwatch fetches results from 14 live
sources at the same time, ranks them, and sends the most relevant ones
to an AI model that writes a short briefing and one recommended action.
While the person reads that, a background agent keeps working: it picks
a specific angle the first pass did not fully cover and searches again.
A separate agent tries to identify named competitors and checks what
they are doing right now.

There is also a lead generation feature. It takes the same search
results and scores each mention for how close that person is to buying
something, then writes a short outreach message ready to send.

A separate, unrelated feature called Website Evolution samples
Common Crawl's historical index to show how a domain's own website has
changed over time. It has nothing to do with brand search and does not
share code with it on purpose.