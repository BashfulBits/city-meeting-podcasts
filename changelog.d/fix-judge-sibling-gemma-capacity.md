- **Judge sibling Gemma capacity.** Gemma sibling packets shrink from 10,000 to 8,500 estimated
  tokens so the Worker's reservation (ratio 1.4 plus forecast output) fits AI Studio's 14,400-token
  minute budget; 10,000 never fit and stranded about 2,600 queued packets on idle keys. New
  subjects now go to the less-loaded of Gemma 31B and 26B (packets waiting, route capacity from the
  Worker's cheap `?routes=1`) and keep the judge that answered them. The sibling outstanding-packet
  limit now applies per judge model, so one model's backlog no longer blocks the others. NVIDIA's
  Gemma 31B route moves
  from 20 to 800 requests a day, held to one of its three shared concurrency slots.
