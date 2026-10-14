# Input sizes for all 60 body-selection recordings

Measured 2026-10-13T16:33:08.196893+00:00. No provider requests were made.

Includes task instructions, full evidence/taxonomy and the response schema. Local official
chat templates are included in the model-specific counts. Groq may add different formatting;
these counts establish feasibility, not live acceptance. Frozen evidence is unchanged.

| Measure | Minimum | Median | Maximum | Total |
|---|---:|---:|---:|---:|
| base_estimated_tokens | 1,332 | 3,187.0 | 6,360 | 191,217 |
| with_schema_estimated_tokens | 1,648 | 3,503.0 | 6,676 | 210,139 |
| gptoss_official_high_chat_tokens | 1,631 | 3,575.5 | 6,645 | 212,470 |
| qwen_official_xhigh_chat_tokens | 1,694 | 3,732.0 | 6,995 | 225,255 |

Configured input ceilings: GPT-OSS 7,125; Qwen 7,000. All 60 are below them locally.
Largest local requests: GPT-OSS 6,645 (480 margin); Qwen 6,995 (5 margin).
The June 13 Dallas Streets packet is borderline on Qwen; May 25 leaves 222 tokens.
A hosted pilot must confirm these largest packets before full evaluation on Qwen.

Generated throughput budgets are GPT-OSS 7,200 and Qwen 6,300 tokens/minute.
These are pacing budgets, separate from per-request input ceilings. Qwen also has a
180,000-token/day generated budget; all 60 local inputs total 225,255 Qwen tokens
before outputs/retries, so evaluation needs daily resets with production headroom.
Provider public defaults can differ from actual organization-specific limits.

See Groq rate limits: https://console.groq.com/docs/rate-limits
See Groq effort mapping: https://console.groq.com/docs/api-reference

| City | UID | Recording title | Estimate with schema | GPT-OSS high | Qwen xhigh |
|---|---|---|---:|---:|---:|
| dallas-tx | 2b172a76868ad86c | 2024 Capital Bond Streets and Transportation Meeting – Jun 13, 2023 | 6,676 | 6,645 | 6,995 |
| dallas-tx | 2bc4c440c974dba8 | 2024 Capital Bond Streets and Transportation Meeting – May 25, 2023 | 6,626 | 6,464 | 6,778 |
| dallas-tx | 130a7ebded8e0e1d | 2024 Bond Task Force Community Bond Task Force Meeting – Aug 15, 2023 | 6,195 | 6,157 | 6,414 |
| dallas-tx | 16300c982b358fe9 | 2024 Capital Bond Streets & Transportation Subcommittee Meeting – Aug 22, 2023 | 6,030 | 5,891 | 6,094 |
| dallas-tx | bd172b762dd20b63 | Saturday Redistricting Commission – Jan 22, 2022 | 5,680 | 5,517 | 5,763 |
| fort-worth-tx | 497b3aa05255bfb9 | BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL BOARD OF TRUSTEES OF EAGLE MOUNTAIN-SAGINAW ISD AND THE FORT WORTH CITY COUNCIL | 5,391 | 5,432 | 5,689 |
| denton-tx | b50c6a018b2c08e7 | Library Board on 2021-05-13 3:30 PM – May 13, 2021 | 4,854 | 4,909 | 5,222 |
| denton-tx | 2b458f3c556df459 | Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-07-22 12:00 PM – Jul 22, 2020 | 4,777 | 5,208 | 5,745 |
| denton-tx | 2493e59c0844a4b4 | Downtown Denton Tax Increment Financing Zone No. 1 Board on 2021-03-24 12:00 PM – Mar 24, 2021 | 4,451 | 4,582 | 4,920 |
| denton-tx | 5a2ce8afb18c6a82 | Downtown Denton Tax Increment Financing Zone No. 1 Board on 2020-10-21 9:00 AM (Special Called) – Oct 21, 2020 | 4,320 | 4,610 | 4,953 |
| denton-tx | 7abd583b2ed8189a | Health & Building Standards Commission on 2021-04-22 3:00 PM – Apr 22, 2021 | 4,232 | 4,431 | 4,776 |
| denton-tx | 1ba31685c3061fad | Animal Shelter Advisory Committee on 2021-03-24 3:00 PM – Mar 24, 2021 | 4,197 | 4,275 | 4,566 |
| fort-worth-tx | 27254aa1a700569f | City Council - Gas Drilling Task Force Workshop of October 14 City Council - Gas Drilling Task Force Workshop of October 14 | 4,195 | 3,918 | 4,088 |
| fort-worth-tx | 0d5832dbd08e6dd8 | City Council - Gas Drilling Task Force Workshop City Council - Gas Drilling Task Force Workshop | 4,168 | 3,888 | 4,056 |
| denton-tx | 280db52d91547dd6 | Health & Building Standards Commission on 2021-06-03 3:00 PM – Jun 03, 2021 | 4,076 | 4,133 | 4,395 |
| denton-tx | 1c91182b70afefb3 | Tax Increment Reinvestment Zone No. 2 Board on 2021-01-13 11:00 AM (Special Called Meeting) – Jan 13, 2021 | 3,941 | 4,116 | 4,406 |
| fort-worth-tx | 00c9f506545d220a | Minority Leaders and Citizen's Council Minority Leaders and Citizen's Council | 3,936 | 3,616 | 3,733 |
| fort-worth-tx | 01127a49d16869ef | Minority Leaders and Citizen's Council Minority Leaders and Citizen's Council | 3,935 | 3,610 | 3,730 |
| fort-worth-tx | 00e5a51984f89df0 | Fort Worth Forward - Episode 15 Fort Worth Forward - Episode 15 | 3,922 | 3,621 | 3,736 |
| fort-worth-tx | 014649318804b782 | Fort Worth Forward - Episode 19 Fort Worth Forward - Episode 19 | 3,922 | 3,618 | 3,741 |
| fort-worth-tx | 018c23a1b2493ae3 | City Page Update City Page Update | 3,891 | 3,587 | 3,693 |
| denton-tx | a1aa4af8f26e120c | Joint Luncheon with Library Board – Mar 03, 2014 | 3,804 | 3,810 | 4,037 |
| addison-tx | 757ab2a4aaf9bbca | TIRZ #1 Board Meeting – Jul 28, 2026 | 3,735 | 3,833 | 4,092 |
| addison-tx | c95af66385f6cf31 | Joint CPAC, P&Z, and City Council Meeting #2 – Jul 11, 2024 | 3,663 | 3,747 | 4,006 |
| addison-tx | fcc9bde16c7f47fe | Joint CPAC, P&Z, and City Council Meeting #1 – Jan 24, 2024 | 3,589 | 3,532 | 3,759 |
| denton-tx | 04689e3929ac104e | Special Citizens Bond Advisory Committee on 2019-06-06 6:00 PM – Jun 06, 2019 | 3,575 | 3,686 | 3,924 |
| addison-tx | 52ec3e810707f814 | Planning & Zoning Commission Work Session – Jan 18, 2022 | 3,563 | 3,702 | 4,050 |
| denton-tx | f3eac6548e0da80c | Animal Shelter Advisory Committee on 2020-10-21 3:00 PM – Oct 21, 2020 | 3,561 | 3,703 | 3,932 |
| addison-tx | e2e2e4d8c9b5330c | Planning & Zoning Commission Work Session – Dec 21, 2021 | 3,556 | 3,661 | 3,996 |
| addison-tx | 4b69d73512792a44 | Planning & Zoning Commission Work Session – Nov 16, 2021 | 3,512 | 3,669 | 4,034 |
| denton-tx | 3afe3a4c3aaa04d9 | Animal Shelter Advisory Committee on 2020-07-23 2:00 PM – Jul 23, 2020 | 3,494 | 3,657 | 3,883 |
| denton-tx | c10f3f6cd97ac38f | Library Board on 2021-04-08 3:30 PM – Apr 08, 2021 | 3,486 | 3,540 | 3,731 |
| addison-tx | ff8eac28f4881cd2 | Planning & Zoning Commission Work Session – Feb 15, 2022 | 3,398 | 3,556 | 3,861 |
| denton-tx | 43599790450713d2 | The League of Women Voters Candidate Forum – Apr 09, 2026 | 3,363 | 3,553 | 3,680 |
| denton-tx | f9c0123089539a11 | The League of Women Voters Candidate Forum – Apr 16, 2026 | 3,363 | 3,564 | 3,697 |
| addison-tx | 902400566a6637b5 | Bond Advisory Committee – Jan 08, 2026 | 3,297 | 3,383 | 3,591 |
| addison-tx | 4ef85ec6540cf95b | Joint P&Z and City Council Meeting #1 – Oct 17, 2023 | 3,237 | 3,301 | 3,519 |
| addison-tx | cb34bf9c6001c32a | Bond Advisory Committee – Dec 04, 2025 | 3,193 | 3,433 | 3,691 |
| addison-tx | 31b194e3b25f0bf8 | Bond Advisory Committee – Oct 30, 2025 | 3,185 | 3,346 | 3,538 |
| addison-tx | 1ef623b5f2bc909a | Bond Advisory Committee – Nov 20, 2025 | 3,156 | 3,331 | 3,602 |
| arlington-tx | 30b28efc23346a6d | Community and Neighborhood Development Committee | 2,925 | 2,990 | 3,299 |
| addison-tx | 63b22f80ce9500ff | Citizen Advisory Committee – Jun 01, 2007 | 2,858 | 2,776 | 2,971 |
| denton-tx | 345a3a95d050eddf | League of Women Voters Candidate Forum – Apr 14, 2022 | 2,805 | 2,990 | 3,163 |
| addison-tx | e59549e833479e1d | Combined Meeting – Oct 08, 2013 | 2,695 | 2,756 | 2,964 |
| arlington-tx | 2d5dc451df2da130 | Environmental Task Force Meeting | 2,608 | 2,821 | 3,073 |
| denton-tx | d38106e9b2725a87 | The League of Women Voters Candidate Forum – Apr 17, 2025 | 2,522 | 2,608 | 2,722 |
| denton-tx | 93bbf0722bbc0edd | League of Women Voters Candidate Forum – Apr 20, 2023 | 2,514 | 2,591 | 2,701 |
| denton-tx | f1e199354d821412 | League of Women Voters Candidate Forum – Apr 04, 2024 | 2,513 | 2,587 | 2,708 |
| denton-tx | c33b73995d47d8f3 | League of Women Voters Candidate Forum – Apr 18, 2018 | 2,390 | 2,420 | 2,491 |
| arlington-tx | 35b22fcc8d07967b | Environmental Task Force Meeting | 2,215 | 2,253 | 2,431 |
| addison-tx | 35a78fa89c7c5c5c | Addison Economic Development Luncheon – Jun 02, 2017 | 2,182 | 2,224 | 2,392 |
| arlington-tx | 25e9b8510b0a7a66 | Environmental Task Force Meeting | 2,059 | 2,187 | 2,340 |
| arlington-tx | 4357f0c39d1ae58f | Community and Neighborhood Development Committee | 2,047 | 2,137 | 2,291 |
| arlington-tx | 237c28f30496e3e0 | Community and Neighborhood Development Committee | 2,031 | 2,126 | 2,276 |
| arlington-tx | d8f3b672b482369b | Community and Neighborhood Development Committee | 2,028 | 2,123 | 2,276 |
| arlington-tx | 1792515fdd51c6c2 | Environmental Task Force | 1,989 | 2,098 | 2,250 |
| addison-tx | 235c1c030568dac0 | George Herbert Walker Bush Elementary Dedication – Dec 09, 2011 | 1,668 | 1,640 | 1,698 |
| addison-tx | 1f50e2274a6dd26c | City Council Swearing-in Ceremony – May 26, 2009 | 1,649 | 1,631 | 1,694 |
| addison-tx | 24d5e7dc230eb900 | Spruill Park Dedication Ceremony – Sep 20, 2009 | 1,648 | 1,637 | 1,701 |
| addison-tx | ee80c45122c4f9f0 | Beckert Park Dedication Ceremony – Jun 06, 2009 | 1,648 | 1,640 | 1,698 |


Catalog follow-up: Groq currently documents Qwen maximum output16,384, while our
catalog records65,536. This does not change the input audit; any later pilot must
use the provider-supported output bound and verify current account limits.
https://console.groq.com/docs/model/qwen/qwen3.8-27b
