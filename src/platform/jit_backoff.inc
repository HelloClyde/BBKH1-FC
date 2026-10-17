/* Generic performance selection only. Existing guest/event semantics remain. */
enum { BACKOFF_RUN=0, BACKOFF_COOL=1, BACKOFF_PROBE=2 };
enum { BACKOFF_PROBE_BYTES=65536, BACKOFF_PROBE_COMPILES=16 };

static void backoff_reset(void)
{ memset(&h1_jit_backoff_state,0,sizeof(h1_jit_backoff_state)); }

static void backoff_baseline(void)
{
    h1_jit_backoff_policy *s=&h1_jit_backoff_state.policy;
    s->initialized=1;
    if(!s->next_delay)s->next_delay=120;
    s->last_epoch=h1_jit_epoch;s->last_wraps=h1_jit_wraps;
    s->last_compiles=h1_jit_stats[0];s->last_emitted=h1_jit_emitted_bytes;
    s->last_invalidations=h1_jit_stats[6];
    s->have_wrap=0;s->stable_frames=0;s->cheap_streak=0;
}
static void backoff_enter(void)
{
    h1_jit_backoff_policy *s=&h1_jit_backoff_state.policy;
    s->mode=BACKOFF_COOL;s->cooldown_left=s->next_delay;
    s->next_delay=s->next_delay<900?s->next_delay*2:1800;
    s->stable_frames=0;s->probe_frames=0;s->cheap_streak=0;
    s->recovery_left=0;++s->trips;h1_jit_dispatch_disabled=1;
}
static void backoff_frame_begin(void)
{
    h1_jit_backoff_policy *s=&h1_jit_backoff_state.policy;
    if(h1_jit_native_depth || s->frame_active)return;
    s->frame_active=1;s->frame_compiles=h1_jit_stats[0];
    s->frame_emitted=h1_jit_emitted_bytes;s->frame_native_ops=h1_jit_stats[3];
    s->frame_wraps=h1_jit_wraps;s->frame_invalidations=h1_jit_stats[6];
    s->frame_epoch=h1_jit_epoch;s->budget_hit=0;
    s->budget_active=s->mode==BACKOFF_PROBE || s->recovery_left!=0;
    h1_jit_dispatch_disabled=s->mode==BACKOFF_COOL;
}
static void backoff_compile_end(int force)
{
    h1_jit_backoff_policy *s=&h1_jit_backoff_state.policy;
    /* A completed compile is published normally. Do not unwind a native or
     * recursive caller. The next ordinary CPU slice selects C. A further
     * automatic compile returns before touching its entry/heat; force still
     * compiles for backend validation. This is a work budget, not a timer. */
    if(!force && s->frame_active && s->budget_active &&
       h1_jit_stats[0]-s->frame_compiles>=BACKOFF_PROBE_COMPILES &&
       h1_jit_emitted_bytes-s->frame_emitted>=BACKOFF_PROBE_BYTES) {
        s->budget_hit=1;h1_jit_dispatch_disabled=1;
    }
}
static void backoff_frame_end(void)
{
    h1_jit_backoff_policy *s=&h1_jit_backoff_state.policy;
    if(!s->frame_active || h1_jit_native_depth)return;
    s->frame_active=0;s->budget_active=0;
    if(!h1_jit_entries || !cache || !cache_bytes) { backoff_reset();return; }
    uint32_t wraps=h1_jit_wraps,compiled=h1_jit_stats[0],emitted=h1_jit_emitted_bytes;
    uint32_t dw=wraps-s->last_wraps,dc=compiled-s->last_compiles;
    uint32_t db=emitted-s->last_emitted,di=h1_jit_stats[6]-s->last_invalidations;
    uint32_t fc=compiled-s->frame_compiles,fb=emitted-s->frame_emitted;
    uint32_t fn=h1_jit_stats[3]-s->frame_native_ops;
    uint32_t fw=wraps-s->frame_wraps,fi=h1_jit_stats[6]-s->frame_invalidations;
    int frame_valid=fi==fw && !(h1_jit_epoch!=s->frame_epoch && !fi) &&
        fc<=0x7fffffffu && fb<=0x7fffffffu && fn<=0x7fffffffu && fw<=0x7fffffffu;
    int valid=s->initialized && di==dw &&
        !(h1_jit_epoch!=s->last_epoch && !di) &&
        dw<=0x7fffffffu && dc<=0x7fffffffu && db<=0x7fffffffu;
    int expensive=s->budget_hit || (frame_valid &&
        fc>=BACKOFF_PROBE_COMPILES && fb>=BACKOFF_PROBE_BYTES);
    if(!valid) { backoff_baseline();dw=0; }
    s->last_epoch=h1_jit_epoch;s->last_wraps=wraps;
    s->last_compiles=compiled;s->last_emitted=emitted;
    s->last_invalidations=h1_jit_stats[6];
    if(s->mode==BACKOFF_COOL) {
        if(s->cooldown_left)--s->cooldown_left;
        if(!s->cooldown_left) {
            s->mode=BACKOFF_PROBE;s->probe_frames=0;
            s->probe_compiles=compiled;s->probe_emitted=emitted;
            ++s->probes;h1_jit_dispatch_disabled=0;
        }
        return;
    }
    ++s->active_frames;
    if(s->mode==BACKOFF_PROBE) {
        ++s->probe_frames; /* Always one complete frame, even if invalidated. */
        if(expensive) { backoff_enter();return; }
        if(frame_valid && fn)++s->cheap_streak;else s->cheap_streak=0;
        if(s->cheap_streak>=8) {
            s->mode=BACKOFF_RUN;s->recovery_left=120;s->stable_frames=0;
            s->have_wrap=0;h1_jit_dispatch_disabled=0;
        } else {
            s->mode=BACKOFF_COOL;s->cooldown_left=30;h1_jit_dispatch_disabled=1;
        }
        return;
    }
    if(s->recovery_left) {
        if(expensive) { backoff_enter();return; }
        --s->recovery_left;
    }
    if(!valid)return; /* First frame or external invalidation: no churn claim. */
    if(frame_valid && fn) { if(s->stable_frames<120)++s->stable_frames; }
    else s->stable_frames=0;
    if(dw) {
        int thrash=(dw>=2 && dc>=128 && db>=cache_bytes/2) || (s->have_wrap &&
            s->active_frames-s->wrap_frame<=120 &&
            compiled-s->wrap_compiles>=128 && emitted-s->wrap_emitted>=cache_bytes/2);
        s->have_wrap=1;s->wrap_frame=s->active_frames;
        s->wrap_compiles=compiled;s->wrap_emitted=emitted;
        if(thrash) { backoff_enter();return; }
    }
    if(s->stable_frames>=120 && !s->recovery_left)s->next_delay=120;
}
