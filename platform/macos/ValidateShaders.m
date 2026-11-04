#import <Foundation/Foundation.h>
#import "nvn_refl.h"
#include <errno.h>

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 3) return 2;
        NSString *root = @(argv[1]);
        NSArray *names = [[NSFileManager defaultManager] contentsOfDirectoryAtPath:root error:nil];
        if (!names) return 2;
        NSMutableArray *failures = [NSMutableArray array];
        NSUInteger checked = 0, slices = 0, dynamic = 0, atomics = 0;
        for (NSString *name in names) {
            if (![name hasSuffix:@"_b.json"]) continue;
            @autoreleasepool {
                checked++;
                NSData *data = [NSData dataWithContentsOfFile:[root stringByAppendingPathComponent:name]];
                NSDictionary *json = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
                if (![json isKindOfClass:[NSDictionary class]]) {
                    [failures addObject:@{@"key":name, @"error":@"unreadable metadata"}];
                    continue;
                }
                // The actual shader loader handles proven slice-only geometry
                // before calling the ordinary resource reflection parser.
                if ([json[@"stage"] isEqual:@"gs"] && json[@"gs_slice"]) {
                    NSDictionary *slice = json[@"gs_slice"];
                    NSNumber *location = [slice isKindOfClass:[NSDictionary class]] ? slice[@"loc"] : nil;
                    NSString *component = [slice isKindOfClass:[NSDictionary class]] ? slice[@"comp"] : nil;
                    if (![location isKindOfClass:[NSNumber class]] || location.integerValue < 0 || location.integerValue >= 32 ||
                        ![@[@"x", @"y", @"z", @"w"] containsObject:component ?: @""]) {
                        [failures addObject:@{@"key":name, @"error":@"invalid layer-only geometry metadata"}];
                        continue;
                    }
                    slices++;
                    continue;
                }
                nvn_refl reflection; char reason[512] = {0};
                int failed = nvn_refl_from_json(json, &reflection, reason, sizeof reason);
                if (failed) [failures addObject:@{@"key":name, @"error":@(reason)}];
                else {
                    dynamic += reflection.dyn != 0;
                    atomics += reflection.image_atomic != 0;
                    free(reflection.c1);
                }
            }
        }
        NSDictionary *result = @{@"metadata_count":@(checked), @"slice_geometry":@(slices),
            @"dynamic_handle_shaders":@(dynamic), @"native_image_atomic_shaders":@(atomics),
            @"failure_count":@(failures.count), @"failures":failures};
        NSError *error = nil;
        NSData *output = [NSJSONSerialization dataWithJSONObject:result options:NSJSONWritingPrettyPrinted error:&error];
        if (!output) { fprintf(stderr, "JSON serialization failed: %s\n", error.description.UTF8String); return 2; }
        FILE *file = fopen(argv[2], "wb");
        if (!file) { fprintf(stderr, "output open failed: %s\n", strerror(errno)); return 2; }
        size_t written = fwrite(output.bytes, 1, output.length, file);
        if (fclose(file) || written != output.length) { fprintf(stderr, "output write failed\n"); return 2; }
        printf("metadata=%lu slices=%lu dynamic=%lu native_atomic=%lu failures=%lu\n",
            checked, slices, dynamic, atomics, failures.count);
        return failures.count ? 1 : 0;
    }
}
