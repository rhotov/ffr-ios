# Hong Kong update 1.0.1, measured from main b7f965b9d7227db8e29f04e84ea7331b.
# nninitStartup forms the allocator object at main+0x77c6210, then calls
# main+0xae770. That constructor reserves 38 GiB and installs the 32-slot
# UE5_6 vtable at main+0x67df800. hle_uemalloc.c installs the host allocator
# before entry, keyed to this build. Every patch checks its original bytes.
CUTS = {
    'b7f965b9d7227db8e29f04e84ea7331b': [
        (0x137f0, 'e06b0294', '1f2003d5',
         'HK nninitStartup: bl 0xae770 (FMallocBinned3 constructor) -> nop; build-gated host allocator supplies the object'),
    ],
}
