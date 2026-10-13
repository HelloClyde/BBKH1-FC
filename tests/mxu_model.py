"""Independent MXU1 subset for Unicorn (which has no Ingenic extension).

Models lane semantics, not physical pipeline timing. Full H1 QEMU validation
uses QEMU's separate MXU decoder. Reject unexpected encodings and masked-state
violations rather than treating unknown instructions as NOPs.
"""
import struct
from unicorn import UC_HOOK_CODE
from unicorn.mips_const import UC_MIPS_REG_0,UC_MIPS_REG_PC,UC_MIPS_REG_CP0_STATUS

def install(machine,payload):
    marker=payload.find(struct.pack('<I',0x48314d58))
    if marker<0:return None
    end=payload.index(struct.pack('<I',0x48314d59),marker+4)
    state={'xr':[0]+[(i*0x1234abcd)&0xffffffff for i in range(1,16)]+[0], 'operations':0,'calls':0}
    uc=machine.uc
    def register(n):return uc.reg_read(UC_MIPS_REG_0+n)
    def hook(uc,address,size,data):
        word=struct.unpack('<I',machine.read(address,4))[0]
        if address==0x83c00020+marker+4:
            state['saved']=(list(state['xr']),uc.reg_read(UC_MIPS_REG_CP0_STATUS))
            state['calls']+=1
        if word==0x03e00008:
            assert (state['xr'],uc.reg_read(UC_MIPS_REG_CP0_STATUS))==state['saved'],'MXU/IRQ state not restored'
        if word>>26!=28 or word&63==2:return
        op=word&63;xr=state['xr'];state['operations']+=1
        a=(word>>6)&15;b=(word>>10)&15;c=(word>>14)&15;d=(word>>18)&15
        assert uc.reg_read(UC_MIPS_REG_CP0_STATUS)&1==0,'MXU state exposed to IRQ'
        if op in (0x2e,0x2f):
            n=(word>>6)&31;reg=(word>>16)&31
            assert n<=16
            if op==0x2e:uc.reg_write(UC_MIPS_REG_0+reg,xr[n])
            elif n:xr[n]=register(reg)&0xffffffff
        else:
            assert xr[16]&1,'MXU operation while disabled'
            if op in (0x10,0x11):
                offset=(word>>10)&1023
                if offset&512:offset-=1024
                addr=(register((word>>21)&31)+offset*4)&0xffffffff
                if op==0x10:xr[a]=struct.unpack('<I',machine.read(addr,4))[0]
                else:machine.word(addr,xr[a])
            elif op in (0x38,0x3a):
                assert (word>>22)&15==0,'Unexpected signed/accumulate pattern'
                products=[((xr[b]>>(8*i))&255)*((xr[c]>>(8*i))&255) for i in range(4)]
                if op==0x3a:
                    acc=[xr[d]&65535,xr[d]>>16,xr[a]&65535,xr[a]>>16]
                    products=[(v+p)&65535 for v,p in zip(acc,products)]
                xr[d]=products[0]|(products[1]<<16)
                xr[a]=products[2]|(products[3]<<16)
            elif op==0x35:
                shift=(word>>22)&15
                vb,vc=xr[b],xr[c]
                xr[a]=((vb&65535)>>shift)|((vb>>16>>shift)<<16)
                xr[d]=((vc&65535)>>shift)|((vc>>16>>shift)<<16)
            elif op==0x3d:
                assert word>>24&3==1,'Unexpected shuffle'
                vb,vc=xr[b],xr[c]
                xr[a]=(vb&0xff000000)|((vb&0xff00)<<8)|((vc>>8)&255)|((vc>>16)&0xff00)
                xr[d]=(vc&255)|((vb&255)<<16)|((vb&0xff0000)<<8)|((vc>>8)&0xff00)
            else:raise AssertionError('Unknown MXU opcode '+hex(word))
        xr[0]=0
        uc.reg_write(UC_MIPS_REG_PC,address+4)
    uc.hook_add(UC_HOOK_CODE,hook,begin=0x83c00020+marker+4,end=0x83c00020+end-1)
    return state
